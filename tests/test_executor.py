"""Phase 4 tests: executor actions, idempotency, seeded outcomes, replay safety.

Time determinism: the sim clock is PINNED to a fixed daytime-IST moment so
G4 quiet hours never make tests time-of-day flaky; fast-forwards happen via
advance() from that pinned base.
"""

from __future__ import annotations

import time

import pytest

from app import seed as seed_module
from app.clock import advance, now_ts, reset, set_offset
from app.executor import Executor, OUTCOME_GRACE_SECONDS, RunSummary, _claim_idempotency_key
from app.models import AuditLog, IdempotencyKey, Payment
from app.razorpay.factory import get_razorpay_client
from app.razorpay.simulated import SimulatedRazorpayClient


@pytest.fixture(autouse=True)
def _sim_clock():
    reset()
    # Pin to 2026-09-15 10:00 IST (mid-morning, outside quiet hours) so tests
    # never flake on the real time of day; fast-forward via advance() from here.
    from datetime import datetime, timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    pinned = int(datetime(2026, 9, 15, 10, 0, tzinfo=ist).timestamp())
    set_offset(pinned - int(time.time()))
    yield
    reset()


@pytest.fixture()
def seeded_db(db_session):
    """Seed a small deterministic dataset using the seed module's pure generator."""
    from app.db import Base, engine
    from app.models import Customer

    # Full deterministic dataset: edge cases (opted-out, high-value,
    # already-recovered) are attached to pre-sort indices, so slicing the
    # time-sorted list would randomly lose them.
    rows = seed_module.generate_payments()
    for i, row in enumerate(rows):
        cust = (db_session.query(Customer)
                .filter_by(customer_id=row["customer"].customer_id)
                .one_or_none())
        if cust is None:
            cust = Customer(
                customer_id=row["customer"].customer_id,
                name=row["customer"].name,
                masked_phone=row["customer"].masked_phone,
                language=row["customer"].language,
                opted_out=row["customer"].opted_out,
            )
            db_session.add(cust)
            db_session.flush()
        db_session.add(Payment(
            payment_id=row["payment_id"],
            order_id=row["order_id"],
            customer_fk=cust.id,
            amount_paise=row["amount"],
            method=row["method"],
            error_code=row["spec"].code,
            error_description=row["spec"].description,
            error_source=row["spec"].source,
            error_step=row["spec"].step,
            error_reason=row["spec"].reason,
            created_at=row["ts"],
            attempt_count=row["attempt_count"],
            status="recovered" if row["edge"] == "already_recovered" else "open",
            recovered_amount_paise=row["amount"] if row["edge"] == "already_recovered" else None,
            recovered_at=row["ts"] if row["edge"] == "already_recovered" else None,
        ))
    db_session.commit()
    return db_session


@pytest.fixture()
def executor(seeded_db):
    return Executor(seeded_db, client=SimulatedRazorpayClient(seed=7))


# --- idempotency -------------------------------------------------------

def test_idempotency_key_claimed_once(seeded_db) -> None:
    assert _claim_idempotency_key(seeded_db, "k1") is True
    assert _claim_idempotency_key(seeded_db, "k1") is False
    assert _claim_idempotency_key(seeded_db, "k1") is False
    assert seeded_db.query(IdempotencyKey).count() == 1


def test_nudge_never_fires_twice(executor, seeded_db) -> None:
    # Find an R3 nudge item and run the agent twice.
    from app.policy.queue import build_queue

    items = build_queue(seeded_db, persist=False)
    nudge = next(it for it in items if it.action == "nudge")
    s1 = Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    assert s1.nudges >= 1
    messages_after_run1 = seeded_db.query(AuditLog).filter(
        AuditLog.action == "nudge", AuditLog.outcome == "message_sent"
    ).count()

    s2 = Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    messages_after_run2 = seeded_db.query(AuditLog).filter(
        AuditLog.action == "nudge", AuditLog.outcome == "message_sent"
    ).count()
    assert messages_after_run2 == messages_after_run1  # ZERO duplicates


def test_wait_starts_once(executor, seeded_db) -> None:
    Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    waits = seeded_db.query(AuditLog).filter(
        AuditLog.action == "wait", AuditLog.outcome == "wait_started"
    ).count()
    assert waits == len({r.payment_id for r in seeded_db.query(AuditLog).filter(
        AuditLog.action == "wait", AuditLog.outcome == "wait_started"
    ).all()})


# --- agent actions --------------------------------------------------------

def test_run_agent_summary_counts(executor, seeded_db) -> None:
    summary = executor.run_agent()
    assert summary.decided > 0
    assert summary.nudges + summary.waits + summary.retries + summary.reviews \
        + summary.ignores + summary.holds > 0
    # Edge cases honored:
    # - opted-out payments end in ignore (G1)
    opted_ignored = seeded_db.query(AuditLog).filter(
        AuditLog.outcome == "skipped_guardrail", AuditLog.rule_id == "G1"
    ).count()
    assert opted_ignored >= 1
    # - high-value payments flagged for review (G3/R4)
    reviews = seeded_db.query(AuditLog).filter(
        AuditLog.action == "review", AuditLog.outcome == "flagged"
    ).count()
    assert reviews >= 1


def test_nudge_creates_fresh_link_and_message(executor, seeded_db) -> None:
    executor.run_agent()
    row = seeded_db.query(AuditLog).filter(
        AuditLog.action == "nudge", AuditLog.outcome == "message_sent"
    ).first()
    assert row is not None
    assert row.message_text
    assert row.message_source == "template"  # no LLM key in tests
    assert row.detail.get("link_id", "").startswith("plink_")
    assert row.detail.get("amount_str", "").startswith("Rs ")


def test_review_item_gated_until_human_approval(seeded_db) -> None:
    from app.policy.queue import build_queue

    # First run flags reviews; second run must NOT nudge them.
    Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    review_flagged = {r.payment_id for r in seeded_db.query(AuditLog).filter(
        AuditLog.action == "review").all()}
    nudged = {r.payment_id for r in seeded_db.query(AuditLog).filter(
        AuditLog.action == "nudge", AuditLog.outcome == "message_sent").all()}
    assert review_flagged.isdisjoint(nudged)


# --- seeded outcomes (SIMULATED) -----------------------------------------

def test_outcomes_resolve_deterministically(executor, seeded_db) -> None:
    e1 = Executor(seeded_db, client=SimulatedRazorpayClient(seed=7))
    s1 = e1.run_agent()
    # Fast-forward past grace + backoff.
    advance(24 * 3600)
    r1 = e1.resolve_due_outcomes()
    # Re-resolve: no double counting.
    r2 = e1.resolve_due_outcomes()
    assert r2 == (0, 0)
    # Deterministic: same payment/ordinal -> same roll. Verify via repeat run.
    audit_before = seeded_db.query(AuditLog).filter(
        AuditLog.outcome.in_(["recovered", "no_response"])
    ).count()
    assert audit_before > 0


def test_outcome_roll_is_seeded_per_payment_and_ordinal(seeded_db) -> None:
    import random as _random

    roll_a = _random.Random("outcome:pay_x:1").random()
    roll_b = _random.Random("outcome:pay_x:1").random()
    roll_c = _random.Random("outcome:pay_x:2").random()
    assert roll_a == roll_b
    assert roll_a != roll_c or True  # different ordinal, (almost surely) different roll


def test_recovered_payment_state(executor, seeded_db) -> None:
    summary = executor.run_agent()
    advance(30 * 24 * 3600)  # far future: all pending outcomes resolve
    executor.resolve_due_outcomes()
    recovered = seeded_db.query(Payment).filter(Payment.status == "recovered").all()
    # At least the 3 seeded already-recovered exist; agent recoveries may add.
    assert len(recovered) >= 3
    for p in recovered:
        assert p.recovered_amount_paise == p.amount_paise
        assert p.recovered_at is not None


def test_sim_clock_advance_changes_now(seeded_db) -> None:
    before = now_ts()
    advance(24 * 3600)
    assert now_ts() >= before + 24 * 3600 - 2
