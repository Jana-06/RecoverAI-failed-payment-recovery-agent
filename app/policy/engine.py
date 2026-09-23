"""The deterministic policy engine — the brain.

PURE FUNCTIONS: no DB, no network, no clock reads — time comes in via
`now_ts`. Every decision returns {action, rule_id, reason, inputs} so the
audit trail can explain exactly why.

Rules (in the buildathon spec):
  R1 technical/transient  -> auto-retry same method after backoff, max 2 retries
  R2 insufficient_funds   -> wait 24h, then nudge with fresh link (alternate method)
  R3 customer-action      -> nudge immediately with fresh link
  R4 risk/blocked         -> NEVER auto-recover; flag for human review

Guardrails (evaluated in order, first match wins):
  G1 opted-out customer            -> ignore (never contact)
  G2 max recovery attempts (3)     -> ignore
  G3 high value (> Rs 25,000)      -> review (human approval before anything)
  G4 quiet hours (21:00-08:00 IST) -> hold contact until 08:00 IST
  G5 daily message budget exhausted-> hold message until next day

Design choice: auto-retries (R1) are customer-visible charge attempts
(UPI collect pings the customer), so quiet hours apply to them too —
conservative by design. Retries are NOT messages, so G5 doesn't apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.policy.classify import REVIEWABLE, Classification
from app.policy.probability import get_probability
from app.utils import ist_hour, ts_to_ist

# --- actions ---
ACTION_RETRY = "retry"      # auto-retry same method (R1)
ACTION_WAIT = "wait"        # do nothing until time_ts (R2 first leg)
ACTION_NUDGE = "nudge"      # send message + fresh payment link (R2 late, R3)
ACTION_IGNORE = "ignore"    # do nothing, permanently (guardrail exhaustion)
ACTION_REVIEW = "review"    # human approval required (R4, high value)
ACTION_HOLD = "hold"        # temporarily blocked (quiet hours / budget)

# --- R1 backoff schedule (documented assumption: transient errors clear in
# minutes; retry once after 30 min, once more after 2 h) ---
R1_RETRY_DELAYS_SECONDS = (30 * 60, 2 * 60 * 60)

# --- guardrail constants ---
MAX_RECOVERY_ATTEMPTS = 3            # hard cap, any combination of retries+nudges
QUIET_HOURS_START = 21               # 21:00 IST inclusive
QUIET_HOURS_END = 8                  # 08:00 IST exclusive
R2_WAIT_SECONDS = 24 * 3600          # R2: wait 24h before first nudge
# G6: minimum spacing between customer-visible actions (retry or nudge).
# Without it, an hourly cron would ping the customer repeatedly.
CONTACT_COOLDOWN_SECONDS = 4 * 3600


@dataclass
class PolicyInput:
    """Everything the engine needs to decide one payment. No DB objects."""

    payment_id: str
    amount_paise: int
    method: str
    classification: Classification
    customer_opted_out: bool
    recovery_attempts: int          # agent-made attempts so far (retries + nudges)
    messages_sent_today: int
    daily_budget: int
    high_value_paise: int
    now_ts: int
    # True when a human has approved this payment in the UI (clears G3,
    # and unlocks a one-off manual nudge for R4 risk items).
    human_approved: bool = False
    # Epoch of the last customer-visible action (retry or nudge) for G6.
    last_action_ts: int | None = None
    # Epoch when an R2 wait was started (None = not waiting yet). Lets the
    # engine distinguish "wait not begun" from "24h elapsed, nudge now".
    last_wait_ts: int | None = None


@dataclass
class PolicyDecision:
    action: str
    rule_id: str
    reason: str
    inputs: dict = field(default_factory=dict)
    # scoring (0 for ignore/hold)
    estimated_probability: float = 0.0
    expected_recovery_paise: int = 0
    # when a wait/hold/retry becomes eligible (epoch seconds), None if N/A
    eligible_at_ts: int | None = None


def _next_quiet_end(now: int) -> int:
    """Epoch seconds of the next 08:00 IST at/after `now`."""
    ist = ts_to_ist(now)
    end_today = ist.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
    if ist >= end_today:
        end_today = end_today + timedelta(days=1)
    return int(end_today.timestamp())


def is_quiet_hours(now: int) -> bool:
    hour = ist_hour(now)
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def _score(
    amount_paise: int, reason: str, method: str, attempts: int, horizon: str
) -> tuple[float, int]:
    """(probability, expected_recovery_paise) for the given horizon."""
    p24, p7 = get_probability(reason, method, attempts)
    p = p7 if horizon == "7d" else p24
    return p, int(amount_paise * p)


def decide(inp: PolicyInput) -> PolicyDecision:
    """Decide the action for one failed payment. Pure, total, unit-tested."""
    cat = inp.classification.category
    reason = inp.classification.reason

    inputs = {
        "payment_id": inp.payment_id,
        "amount_paise": inp.amount_paise,
        "method": inp.method,
        "category": cat,
        "reason": reason,
        "opted_out": inp.customer_opted_out,
        "recovery_attempts": inp.recovery_attempts,
        "messages_sent_today": inp.messages_sent_today,
        "daily_budget": inp.daily_budget,
        "now_ts": inp.now_ts,
        "last_action_ts": inp.last_action_ts,
        "last_wait_ts": inp.last_wait_ts,
        "signal": inp.classification.signal,
    }

    # --- G1: opted-out customers are never contacted (wins over everything
    # contact-related; R4 review is internal, but ignore is the safest union). ---
    if inp.customer_opted_out:
        return PolicyDecision(
            action=ACTION_IGNORE, rule_id="G1",
            reason="customer opted out of recovery contact",
            inputs=inputs,
        )

    # --- G2: hard cap on total recovery attempts ---
    if inp.recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
        return PolicyDecision(
            action=ACTION_IGNORE, rule_id="G2",
            reason=f"max recovery attempts reached ({MAX_RECOVERY_ATTEMPTS})",
            inputs=inputs,
        )

    # --- R4 / G3: anything needing a human ---
    if cat in REVIEWABLE:
        if inp.human_approved:
            # Human override: contact allowed once. Risk categories have no
            # meaningful probability in the table (never auto-recovered), so
            # use a documented conservative manual-override estimate.
            p, expected = 0.10, int(inp.amount_paise * 0.10)
            base = PolicyDecision(
                action=ACTION_NUDGE, rule_id="R4",
                reason=f"human-approved contact for {cat} failure (manual override)",
                inputs=inputs, estimated_probability=p, expected_recovery_paise=expected,
            )
            return _apply_contact_guardrails(inp, inputs, base, is_message=True)
        return PolicyDecision(
            action=ACTION_REVIEW, rule_id="R4",
            reason=f"{cat} failure flagged for human review (never auto-recovered)",
            inputs=inputs,
            estimated_probability=0.0, expected_recovery_paise=0,
        )
    if inp.amount_paise > inp.high_value_paise:
        if not inp.human_approved:
            p, expected = _score(inp.amount_paise, reason, inp.method, inp.recovery_attempts, "7d")
            return PolicyDecision(
                action=ACTION_REVIEW, rule_id="G3",
                reason="amount above high-value threshold requires human approval",
                inputs=inputs,
                estimated_probability=p, expected_recovery_paise=expected,
            )
        # Approved: fall through to the normal category rule below.
        pass

    # --- category rules ---
    if cat == "technical":
        return _decide_technical(inp, inputs)
    if cat == "insufficient_funds":
        return _decide_insufficient_funds(inp, inputs, reason)
    if cat == "customer_action":
        return _decide_customer_action(inp, inputs, reason)

    # Unhandled category (should not happen) -> safe default.
    return PolicyDecision(
        action=ACTION_REVIEW, rule_id="R4",
        reason="unclassified category defaults to human review", inputs=inputs,
    )


def _apply_contact_guardrails(
    inp: PolicyInput,
    inputs: dict,
    decision: PolicyDecision,
    is_message: bool,
) -> PolicyDecision:
    """Apply quiet hours + daily budget to customer-visible actions.

    Returns a HOLD decision (with eligible_at) if blocked; else passes
    `decision` through unchanged.
    """
    if is_quiet_hours(inp.now_ts):
        return PolicyDecision(
            action=ACTION_HOLD, rule_id="G4",
            reason=f"{decision.rule_id} action held: quiet hours (21:00-08:00 IST)",
            inputs=inputs, eligible_at_ts=_next_quiet_end(inp.now_ts),
            estimated_probability=decision.estimated_probability,
            expected_recovery_paise=decision.expected_recovery_paise,
        )
    if is_message and inp.messages_sent_today >= inp.daily_budget:
        return PolicyDecision(
            action=ACTION_HOLD, rule_id="G5",
            reason=f"{decision.rule_id} message held: daily budget "
                   f"({inp.messages_sent_today}/{inp.daily_budget}) exhausted",
            inputs=inputs, eligible_at_ts=_next_quiet_end(inp.now_ts),
            estimated_probability=decision.estimated_probability,
            expected_recovery_paise=decision.expected_recovery_paise,
        )
    # G6 cooldown applies to messages only: R1 retries have their own designed
    # backoff schedule, enforced by the executor via eligible_at_ts.
    if (
        is_message
        and inp.last_action_ts is not None
        and inp.now_ts < inp.last_action_ts + CONTACT_COOLDOWN_SECONDS
    ):
        return PolicyDecision(
            action=ACTION_HOLD, rule_id="G6",
            reason=f"{decision.rule_id} message held: contact cooldown (4h between "
                   f"customer messages)",
            inputs=inputs,
            eligible_at_ts=(inp.last_action_ts or 0) + CONTACT_COOLDOWN_SECONDS,
            estimated_probability=decision.estimated_probability,
            expected_recovery_paise=decision.expected_recovery_paise,
        )
    return decision


def _decide_technical(inp: PolicyInput, inputs: dict) -> PolicyDecision:
    # R1: retry same method after backoff; max 2 retries (and always <= G2 cap).
    retries_done = inp.recovery_attempts  # R1 retries are the only attempts so far
    if retries_done >= len(R1_RETRY_DELAYS_SECONDS):
        return PolicyDecision(
            action=ACTION_IGNORE, rule_id="R1",
            reason="transient failure: 2 auto-retries already exhausted", inputs=inputs,
        )
    delay = R1_RETRY_DELAYS_SECONDS[retries_done]
    eligible = inp.now_ts + delay
    p, expected = _score(inp.amount_paise, inp.classification.reason, inp.method,
                         inp.recovery_attempts, "24h")
    base = PolicyDecision(
        action=ACTION_RETRY, rule_id="R1",
        reason=f"transient failure ({inp.classification.reason}): auto-retry same method "
               f"#{retries_done + 1} after {delay // 60}m backoff",
        inputs=inputs, eligible_at_ts=eligible,
        estimated_probability=p, expected_recovery_paise=expected,
    )
    # Retry is a customer-visible charge attempt but NOT a message (G5 skips).
    return _apply_contact_guardrails(inp, inputs, base, is_message=False)


def _decide_insufficient_funds(
    inp: PolicyInput, inputs: dict, reason: str
) -> PolicyDecision:
    # R2: wait 24h (payday effect), then nudge with fresh link + alternate method.
    if inp.last_wait_ts is None:
        p, expected = _score(inp.amount_paise, reason, inp.method, 0, "7d")
        base = PolicyDecision(
            action=ACTION_WAIT, rule_id="R2",
            reason="insufficient funds: wait 24h before nudging (payday window)",
            inputs=inputs, eligible_at_ts=inp.now_ts + R2_WAIT_SECONDS,
            estimated_probability=p, expected_recovery_paise=expected,
        )
        # Waiting is not a contact; guardrails don't apply to the wait itself.
        return base

    if inp.now_ts < inp.last_wait_ts + R2_WAIT_SECONDS:
        # Wait already started and hasn't elapsed — keep waiting.
        p, expected = _score(inp.amount_paise, reason, inp.method, 0, "7d")
        return PolicyDecision(
            action=ACTION_WAIT, rule_id="R2",
            reason="insufficient funds: 24h wait in progress (payday window)",
            inputs=inputs, eligible_at_ts=inp.last_wait_ts + R2_WAIT_SECONDS,
            estimated_probability=p, expected_recovery_paise=expected,
        )

    p, expected = _score(inp.amount_paise, reason, inp.method, inp.recovery_attempts, "24h")
    base = PolicyDecision(
        action=ACTION_NUDGE, rule_id="R2",
        reason="24h elapsed: nudge with fresh payment link offering an alternate method",
        inputs=inputs,
        estimated_probability=p, expected_recovery_paise=expected,
    )
    return _apply_contact_guardrails(inp, inputs, base, is_message=True)


def _decide_customer_action(
    inp: PolicyInput, inputs: dict, reason: str
) -> PolicyDecision:
    # R3: nudge immediately with a fresh link.
    p, expected = _score(inp.amount_paise, reason, inp.method, inp.recovery_attempts, "24h")
    base = PolicyDecision(
        action=ACTION_NUDGE, rule_id="R3",
        reason=f"customer-action failure ({reason}): immediate nudge with fresh link",
        inputs=inputs,
        estimated_probability=p, expected_recovery_paise=expected,
    )
    return _apply_contact_guardrails(inp, inputs, base, is_message=True)
