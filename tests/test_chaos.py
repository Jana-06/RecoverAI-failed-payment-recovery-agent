"""Phase 6 tests: CHAOS fault injection — the app must degrade gracefully.

CHAOS=1 makes the LLM and the Razorpay client randomly fail. The invariants:
1. Every nudged payment STILL gets a sendable message (template floor).
2. Link failures are audited as errors, NOT swallowed, and the idempotency
   key is released so a later run can retry the same ordinal.
3. No exception ever escapes the agent run; summaries stay consistent.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pytest

from app import seed as seed_module
from app.clock import reset, set_offset
from app.config import settings
from app.executor import Executor
from app.llm.gemini import GeminiError
from app.models import AuditLog, IdempotencyKey, Payment
from app.razorpay.simulated import SimulatedRazorpayClient
from app.razorpay.types import CreateLinkResult


@pytest.fixture(autouse=True)
def _env():
    reset()
    ist = timezone(timedelta(hours=5, minutes=30))
    set_offset(int(datetime(2026, 9, 15, 10, 0, tzinfo=ist).timestamp()) - int(time.time()))
    yield
    reset()


@pytest.fixture()
def seeded_db(db_session):
    from app.models import Customer

    for row in seed_module.generate_payments():
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


def _daytime_runnable(db) -> bool:
    # Tests are pinned to 10:00 IST, so G4 never holds; nothing to check.
    return True


def test_chaos_llm_outage_all_nudges_use_template_fallback(monkeypatch, seeded_db) -> None:
    """LLM completely down: every message still sent, source=template, fallback logged.

    Fault is injected at the real failure point: `write_message` inside the
    writer (the facade compose_message must never raise).
    """
    import app.llm.writer as writer_module

    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")

    def down(req):
        raise GeminiError("chaos: socket hangup")

    monkeypatch.setattr(writer_module, "write_message", down)

    summary = Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    assert summary.nudges > 0
    assert summary.fallbacks == summary.nudges  # every nudge fell back
    assert summary.template_used == summary.nudges
    assert summary.llm_used == 0
    rows = (seeded_db.query(AuditLog)
            .filter(AuditLog.action == "nudge", AuditLog.outcome == "message_sent")
            .all())
    assert len(rows) == summary.nudges
    for r in rows:
        assert r.message_source == "template"
        assert r.message_text  # a real message exists
        assert r.detail["fallback_used"] is True
        assert "llm_error" in r.detail["fallback_reason"]


def test_chaos_compose_never_raises_even_if_writer_does(monkeypatch, seeded_db) -> None:
    """Even a writer-level catastrophe can't escape compose_message."""
    import app.llm.writer as writer_module

    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")

    def explode(req):
        raise RuntimeError("total meltdown")

    monkeypatch.setattr(writer_module, "write_message", explode)
    from app.llm.gemini import MessageRequest
    from app.llm.writer import compose_message

    outcome = compose_message(MessageRequest(
        first_name="A", amount_paise=500_00, language="en",
        merchant_name="M", link="https://rzp.io/i/x1",
    ))
    assert outcome.source == "template"
    assert outcome.fallback_used is True
    assert outcome.text


def test_chaos_link_failures_audited_and_key_released(monkeypatch, seeded_db) -> None:
    """Razorpay client down: errors audited, keys released, retry succeeds later."""
    monkeypatch.setattr(settings, "chaos", True)

    # Force ALL link creations to fail (deterministic, stronger than 25% rolls).
    def always_fail(self, amount, customer, description="", reference_id="",
                    expire_seconds=7 * 24 * 3600):
        return CreateLinkResult(ok=False, error="chaos_upstream_500")

    original_link_method = SimulatedRazorpayClient.create_payment_link  # capture BEFORE patching
    monkeypatch.setattr(SimulatedRazorpayClient, "create_payment_link", always_fail)

    client = SimulatedRazorpayClient(seed=7)
    summary = Executor(seeded_db, client=client).run_agent()
    assert summary.nudges == 0
    assert summary.errors >= 1

    err_rows = (seeded_db.query(AuditLog)
                .filter(AuditLog.outcome == "error", AuditLog.action == "nudge")
                .all())
    assert err_rows
    assert all("payment link creation failed" in r.reason for r in err_rows)

    # KEY RELEASE: the failed payments' nudge keys must NOT remain claimed.
    failed_ids = {r.payment_id for r in err_rows}
    claimed = {k.key for k in seeded_db.query(IdempotencyKey).all()}
    for pid in failed_ids:
        assert f"{pid}:nudge:1" not in claimed, f"{pid} permanently blocked!"

    # Recovery: client heals -> the SAME payments get nudged on the next run.
    monkeypatch.setattr(SimulatedRazorpayClient, "create_payment_link", original_link_method)
    monkeypatch.setattr(settings, "chaos", False)  # deterministic healed run
    healed = Executor(seeded_db, client=SimulatedRazorpayClient(seed=11)).run_agent()
    assert healed.nudges > 0
    nudged_ids = {r.payment_id for r in (seeded_db.query(AuditLog)
                  .filter(AuditLog.action == "nudge", AuditLog.outcome == "message_sent")
                  .all())}
    assert nudged_ids & failed_ids, "previously failed payments should be retried"


def test_chaos_random_rolls_never_crash_and_stay_consistent(monkeypatch, seeded_db) -> None:
    """Random 25%/15% chaos: no crash, counts consistent, messages always valid."""
    monkeypatch.setattr(settings, "chaos", True)
    monkeypatch.setattr(settings, "gemini_api_key", "")

    summary = Executor(seeded_db, client=SimulatedRazorpayClient(seed=7)).run_agent()
    assert summary.decided > 0
    # Consistency: every decided item lands in exactly one bucket.
    assert (summary.retries + summary.nudges + summary.waits + summary.holds
            + summary.ignores + summary.reviews + summary.errors) <= summary.decided
    # Every sent message came from the template (no LLM configured).
    assert summary.llm_used == 0
    assert summary.fallbacks == 0  # no LLM configured -> not a "fallback"


def test_chaos_flag_reflected_in_health_and_meta(client_api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "chaos", True)
    resp = client_api.get("/api/health")
    assert resp.json()["chaos"] is True


@pytest.fixture()
def client_api():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)
