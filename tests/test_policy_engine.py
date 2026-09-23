"""Phase 2 tests: classification + policy engine rules and guardrails.

The engine is pure, so these tests need no DB — just constructed inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.policy import engine
from app.policy.classify import (
    CATEGORY_CUSTOMER_ACTION,
    CATEGORY_INSUFFICIENT_FUNDS,
    CATEGORY_RISK,
    CATEGORY_TECHNICAL,
    CATEGORY_UNKNOWN,
    classify,
)
from app.policy.engine import (
    ACTION_HOLD,
    ACTION_IGNORE,
    ACTION_NUDGE,
    ACTION_REVIEW,
    ACTION_RETRY,
    ACTION_WAIT,
    MAX_RECOVERY_ATTEMPTS,
    PolicyInput,
    decide,
    is_quiet_hours,
)
from app.policy.probability import get_probability


def make_input(**overrides) -> PolicyInput:
    """A mid-size UPI timeout payment at 14:00 IST, no prior attempts."""
    base = dict(
        payment_id="pay_test",
        amount_paise=500_00,  # Rs 500
        method="upi",
        classification=classify("timeout"),
        customer_opted_out=False,
        recovery_attempts=0,
        messages_sent_today=0,
        daily_budget=200,
        high_value_paise=2_500_000,
        now_ts=1757946600,  # 2025-09-15 14:30 UTC == 20:00 IST (not quiet)
    )
    base.update(overrides)
    return PolicyInput(**base)


# --- classification -------------------------------------------------------

def test_classification_maps_every_seeded_reason() -> None:
    cases = {
        "timeout": CATEGORY_TECHNICAL,
        "bank_technical_error": CATEGORY_TECHNICAL,
        "network_error": CATEGORY_TECHNICAL,
        "insufficient_funds": CATEGORY_INSUFFICIENT_FUNDS,
        "authentication_failed": CATEGORY_CUSTOMER_ACTION,
        "wrong_otp": CATEGORY_CUSTOMER_ACTION,
        "customer_cancelled": CATEGORY_CUSTOMER_ACTION,
        "card_blocked_or_fraud": CATEGORY_RISK,
        "suspected_fraud": CATEGORY_RISK,
    }
    for reason, expected in cases.items():
        assert classify(reason).category == expected, reason


def test_classification_normalizes_spacing_and_case() -> None:
    assert classify("Timeout").category == CATEGORY_TECHNICAL
    assert classify("Insufficient Funds").category == CATEGORY_INSUFFICIENT_FUNDS
    assert classify("CARD BLOCKED").category == CATEGORY_RISK


def test_unknown_reason_defaults_to_conservative_review_category() -> None:
    c = classify("totally_novel_error")
    assert c.category == CATEGORY_UNKNOWN
    # And the engine sends unknowns to human review, never auto-recovery.
    d = decide(make_input(classification=c))
    assert d.action == ACTION_REVIEW
    assert d.rule_id == "R4"


def test_risk_patterns_in_description_route_to_risk_even_with_unknown_code() -> None:
    c = classify("weird_code", error_description="Transaction declined: suspected fraud")
    assert c.category == CATEGORY_RISK


def test_source_step_hint_catches_auth_failures() -> None:
    c = classify("", error_source="bank", error_step="authentication")
    assert c.category == CATEGORY_CUSTOMER_ACTION


# --- rule decisions ---------------------------------------------------------

def test_r1_technical_retries_same_method_with_backoff() -> None:
    d = decide(make_input())
    assert (d.action, d.rule_id) == (ACTION_RETRY, "R1")
    assert d.eligible_at_ts == 1757946600 + engine.R1_RETRY_DELAYS_SECONDS[0]
    assert "retry" in d.reason.lower()


def test_r1_second_retry_then_exhausted() -> None:
    d2 = decide(make_input(recovery_attempts=1))
    assert (d2.action, d2.rule_id) == (ACTION_RETRY, "R1")
    assert d2.eligible_at_ts == 1757946600 + engine.R1_RETRY_DELAYS_SECONDS[1]

    d3 = decide(make_input(recovery_attempts=2))
    assert d3.action == ACTION_IGNORE  # 2 retries done -> R1 gives up


def test_r2_insufficient_funds_waits_24h_then_nudges() -> None:
    d0 = decide(make_input(classification=classify("insufficient_funds")))
    assert (d0.action, d0.rule_id) == (ACTION_WAIT, "R2")
    assert d0.eligible_at_ts == 1757946600 + 24 * 3600

    # Wait started 25h ago -> nudge now (alternate-method nudge).
    d1 = decide(make_input(classification=classify("insufficient_funds"),
                           recovery_attempts=1,
                           last_wait_ts=1757946600 - 25 * 3600))
    assert (d1.action, d1.rule_id) == (ACTION_NUDGE, "R2")


def test_r3_customer_action_nudges_immediately() -> None:
    for reason in ("authentication_failed", "wrong_otp", "customer_cancelled"):
        d = decide(make_input(classification=classify(reason)))
        assert (d.action, d.rule_id) == (ACTION_NUDGE, "R3"), reason
        assert d.eligible_at_ts is None  # immediate


def test_r4_risk_never_auto_recovers() -> None:
    for reason in ("card_blocked_or_fraud", "suspected_fraud"):
        d = decide(make_input(classification=classify(reason)))
        assert d.action == ACTION_REVIEW
        assert d.rule_id == "R4"
        assert d.expected_recovery_paise == 0


# --- guardrails -------------------------------------------------------------

def test_g1_opted_out_customers_are_never_contacted() -> None:
    for classification in (
        classify("timeout"),
        classify("insufficient_funds"),
        classify("authentication_failed"),
        classify("card_blocked_or_fraud"),
    ):
        d = decide(make_input(classification=classification, customer_opted_out=True))
        assert d.action == ACTION_IGNORE
        assert d.rule_id == "G1"


def test_g1_opted_out_wins_over_high_value() -> None:
    d = decide(make_input(amount_paise=90_000_00, customer_opted_out=True))
    assert (d.action, d.rule_id) == (ACTION_IGNORE, "G1")


def test_g2_max_attempts_cap() -> None:
    d = decide(make_input(recovery_attempts=MAX_RECOVERY_ATTEMPTS))
    assert (d.action, d.rule_id) == (ACTION_IGNORE, "G2")
    # Cap applies even to normally-immediate R3 nudges.
    d = decide(make_input(classification=classify("wrong_otp"),
                          recovery_attempts=MAX_RECOVERY_ATTEMPTS))
    assert d.action == ACTION_IGNORE


def test_g3_high_value_requires_human_approval() -> None:
    d = decide(make_input(amount_paise=2_500_001))  # just over Rs 25,000
    assert (d.action, d.rule_id) == (ACTION_REVIEW, "G3")

    # Exactly at the threshold is NOT high value (rule is strictly greater).
    d_at = decide(make_input(amount_paise=2_500_000))
    assert d_at.action != ACTION_REVIEW or d_at.rule_id != "G3"

    # Once approved in the UI, the normal category rule applies.
    d_ok = decide(make_input(amount_paise=2_500_001, human_approved=True))
    assert d_ok.action == ACTION_RETRY  # still a technical failure -> R1


def test_g3_approval_unlocks_r4_manual_nudge() -> None:
    d = decide(make_input(classification=classify("card_blocked_or_fraud"),
                          human_approved=True))
    assert (d.action, d.rule_id) == (ACTION_NUDGE, "R4")
    # Without approval it stays locked.
    d_locked = decide(make_input(classification=classify("card_blocked_or_fraud")))
    assert d_locked.action == ACTION_REVIEW


# --- quiet hours ------------------------------------------------------------

def _ts_at_ist(hour: int, minute: int = 0) -> int:
    """Epoch for 2026-09-15 at the given IST wall time."""
    from app.utils import IST

    dt = datetime(2026, 9, 15, hour, minute, tzinfo=IST)
    return int(dt.timestamp())


def test_quiet_hours_boundaries() -> None:
    assert not is_quiet_hours(_ts_at_ist(20, 59))
    assert is_quiet_hours(_ts_at_ist(21, 0))
    assert is_quiet_hours(_ts_at_ist(23, 30))
    assert is_quiet_hours(_ts_at_ist(3, 15))
    assert is_quiet_hours(_ts_at_ist(7, 59))
    assert not is_quiet_hours(_ts_at_ist(8, 0))


def test_g4_quiet_hours_hold_nudge_until_8am_ist() -> None:
    night = _ts_at_ist(22, 0)
    d = decide(make_input(classification=classify("wrong_otp"), now_ts=night))
    assert (d.action, d.rule_id) == (ACTION_HOLD, "G4")
    # Held until 08:00 IST next morning = 10 hours later.
    assert d.eligible_at_ts is not None
    assert 9 * 3600 <= d.eligible_at_ts - night <= 10 * 3600 + 60


def test_g4_quiet_hours_also_hold_retries_but_not_waits() -> None:
    night = _ts_at_ist(23, 0)
    retry = decide(make_input(now_ts=night))  # technical -> R1 retry
    assert (retry.action, retry.rule_id) == (ACTION_HOLD, "G4")

    wait = decide(make_input(classification=classify("insufficient_funds"),
                             now_ts=night))
    assert (wait.action, wait.rule_id) == (ACTION_WAIT, "R2")  # waits unaffected


def test_g5_daily_budget_holds_messages_but_not_retries() -> None:
    d = decide(make_input(classification=classify("wrong_otp"),
                          messages_sent_today=200, daily_budget=200))
    assert (d.action, d.rule_id) == (ACTION_HOLD, "G5")

    r = decide(make_input(messages_sent_today=200, daily_budget=200))
    assert (r.action, r.rule_id) == (ACTION_RETRY, "R1")  # retries aren't messages


def test_g6_cooldown_holds_messages_within_4h_of_last_contact() -> None:
    # Last contact 08:30 IST; re-check at 10:00 IST (within 4h) -> G6 hold.
    last = _ts_at_ist(8, 30)
    d = decide(make_input(classification=classify("wrong_otp"),
                          last_action_ts=last, now_ts=_ts_at_ist(10, 0)))
    assert (d.action, d.rule_id) == (ACTION_HOLD, "G6")
    assert d.eligible_at_ts == last + engine.CONTACT_COOLDOWN_SECONDS

    # At 12:30 IST (exactly 4h later, daytime) the nudge goes through.
    after = decide(make_input(classification=classify("wrong_otp"),
                              last_action_ts=last, now_ts=_ts_at_ist(12, 30)))
    assert (after.action, after.rule_id) == (ACTION_NUDGE, "R3")


def test_g6_cooldown_does_not_block_retries_or_waits() -> None:
    last = 1757946600 - 30 * 60  # 30m ago
    retry = decide(make_input(last_action_ts=last))
    assert (retry.action, retry.rule_id) == (ACTION_RETRY, "R1")

    wait = decide(make_input(classification=classify("insufficient_funds"),
                             last_action_ts=last))
    assert (wait.action, wait.rule_id) == (ACTION_WAIT, "R2")


def test_r2_wait_in_progress_stays_waiting_until_24h_elapsed() -> None:
    wait_start = 1757946600 - 10 * 3600  # started 10h ago
    d = decide(make_input(classification=classify("insufficient_funds"),
                          last_wait_ts=wait_start))
    assert (d.action, d.rule_id) == (ACTION_WAIT, "R2")
    assert d.eligible_at_ts == wait_start + engine.R2_WAIT_SECONDS

    elapsed = decide(make_input(classification=classify("insufficient_funds"),
                                last_wait_ts=wait_start,
                                now_ts=wait_start + engine.R2_WAIT_SECONDS))
    assert (elapsed.action, elapsed.rule_id) == (ACTION_NUDGE, "R2")


# --- scoring ----------------------------------------------------------------

def test_priority_score_is_amount_times_probability() -> None:
    d = decide(make_input(classification=classify("wrong_otp")))
    # wrong_otp aliases to authentication_failed in the probability table.
    p24, _ = get_probability("wrong_otp", "upi", 0)
    assert p24 == get_probability("authentication_failed", "upi", 0)[0]
    expected = int(500_00 * p24)
    assert d.expected_recovery_paise == expected
    assert 0 < d.estimated_probability <= 1


def test_probability_aliases_specific_reasons_to_canonical_entries() -> None:
    # Specific codes must NOT fall through to the zero unknown row.
    for reason in ("wrong_otp", "3ds_failure", "payment_cancelled_by_user"):
        p, _ = get_probability(reason, "upi", 0)
        assert p > 0, reason


def test_probability_falls_off_with_prior_attempts() -> None:
    p0, _ = get_probability("timeout", "upi", 0)
    p1, _ = get_probability("timeout", "upi", 1)
    p3, _ = get_probability("timeout", "upi", 3)
    assert p0 > p1 > p3 > 0


def test_unknown_method_uses_wildcard() -> None:
    p, _ = get_probability("timeout", "carrier_pigeon", 0)
    assert 0 < p <= 1


def test_decision_shape_is_complete() -> None:
    d = decide(make_input())
    assert d.action and d.rule_id and d.reason
    assert set(d.inputs) >= {
        "payment_id", "amount_paise", "method", "category", "reason",
        "opted_out", "recovery_attempts", "now_ts",
    }


# --- guardrail precedence -----------------------------------------------------

def test_guardrail_order_attempts_before_rules() -> None:
    # G2 (cap) must fire even when the rule would otherwise act.
    d = decide(make_input(classification=classify("wrong_otp"), recovery_attempts=3))
    assert d.rule_id == "G2"
