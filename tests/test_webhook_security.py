"""Phase 1 security tests: webhook signature verification + replay protection."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.webhooks import sign_webhook_body


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _payload(payment_id: str = "pay_hook_001", amount: int = 50_000) -> bytes:
    body = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": f"order_{payment_id}",
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "method": "upi",
                    "error": {
                        "code": "GATEWAY_ERROR",
                        "description": "Payment did not complete within the time limit",
                        "source": "network",
                        "step": "payment_initiation",
                        "reason": "timeout",
                    },
                    "customer_name": "Aarav",
                    "customer_contact": "+919876543210",
                    "created_at": int(time.time()),
                }
            }
        },
    }
    return json.dumps(body).encode()


def test_rejects_missing_signature(client: TestClient, db_session) -> None:
    resp = client.post("/webhook/payment_failed", content=_payload())
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_signature"


def test_rejects_bad_signature(client: TestClient, db_session) -> None:
    resp = client.post(
        "/webhook/payment_failed",
        content=_payload(),
        headers={"X-Razorpay-Signature": "deadbeef" * 8},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_signature"


def test_signature_is_computed_over_raw_body(client: TestClient, db_session) -> None:
    # Sign one body, send another: must be rejected (prevents body-swap attacks).
    sig = sign_webhook_body(_payload(payment_id="pay_other"), settings.razorpay_webhook_secret)
    resp = client.post(
        "/webhook/payment_failed",
        content=_payload(),
        headers={"X-Razorpay-Signature": sig},
    )
    assert resp.status_code == 400


def test_valid_signature_is_accepted_and_ingested(client: TestClient, db_session) -> None:
    body = _payload(payment_id="pay_hook_ok")
    sig = sign_webhook_body(body, settings.razorpay_webhook_secret)
    resp = client.post(
        "/webhook/payment_failed", content=body, headers={"X-Razorpay-Signature": sig}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True and data["created"] is True

    from app.models import Payment
    row = db_session.query(Payment).filter(Payment.payment_id == "pay_hook_ok").one()
    assert row.amount_paise == 50_000
    assert row.error_reason == "timeout"
    # PII check: full phone never stored.
    assert row.customer is not None
    assert "+919876543210" not in (row.customer.masked_phone or "")


def test_replayed_webhook_does_not_duplicate(client: TestClient, db_session) -> None:
    body = _payload(payment_id="pay_hook_replay")
    sig = sign_webhook_body(body, settings.razorpay_webhook_secret)
    headers = {"X-Razorpay-Signature": sig}
    r1 = client.post("/webhook/payment_failed", content=body, headers=headers)
    r2 = client.post("/webhook/payment_failed", content=body, headers=headers)
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False  # deduped

    from app.models import Payment
    count = db_session.query(Payment).filter(Payment.payment_id == "pay_hook_replay").count()
    assert count == 1


def test_non_payment_failed_event_is_acked_not_processed(client: TestClient, db_session) -> None:
    body = json.dumps({"event": "refund.processed", "payload": {}}).encode()
    sig = sign_webhook_body(body, settings.razorpay_webhook_secret)
    resp = client.post(
        "/webhook/payment_failed", content=body, headers={"X-Razorpay-Signature": sig}
    )
    assert resp.status_code == 200
    assert resp.json()["handled"] is False
