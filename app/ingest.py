"""Webhook ingest: normalize Razorpay payment.failed entities into our schema.

Replay-safe: the same webhook delivered twice (Razorpay retries on non-2xx)
results in one payment row. PII is minimized on the way in: full phone
numbers are never persisted, only masked last-4.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.clock import now_ts
from app.models import AuditLog, Customer, Payment
from app.utils import mask_phone
from app.webhooks import WebhookPaymentEntity


def _anon_customer_id(contact: str, payment_id: str) -> str:
    """Deterministic non-PII customer key from the phone number."""
    basis = contact or payment_id  # fall back to payment id if no contact
    return "cust_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def upsert_failed_payment(db: Session, entity: WebhookPaymentEntity) -> tuple[Payment, bool]:
    """Insert a failed payment if new; return (payment, created).

    Returns the existing row on replay (created=False) — callers must not
    double-count or re-decide on replays.
    """
    existing = db.query(Payment).filter(Payment.payment_id == entity.id).one_or_none()
    if existing is not None:
        return existing, False

    if not entity.id or entity.amount <= 0:
        raise ValueError("webhook entity missing id or amount")

    cust_id = _anon_customer_id(entity.customer_contact, entity.id)
    customer = (
        db.query(Customer).filter(Customer.customer_id == cust_id).one_or_none()
    )
    if customer is None:
        customer = Customer(
            customer_id=cust_id,
            name=entity.customer_name or "Customer",
            masked_phone=mask_phone(entity.customer_contact),
            language="en",  # default; a real system would infer from profile
            opted_out=False,
        )
        db.add(customer)
        db.flush()

    payment = Payment(
        payment_id=entity.id,
        order_id=entity.order_id or f"order_{entity.id}",
        customer=customer,
        amount_paise=entity.amount,
        currency=entity.currency or "INR",
        method=entity.method or "",
        error_code=entity.error.code,
        error_description=entity.error.description,
        error_source=entity.error.source,
        error_step=entity.error.step,
        error_reason=entity.error.reason,
        created_at=entity.created_at or _now_ts(),
        attempt_count=1,
        status="open",
    )
    db.add(payment)
    db.flush()

    db.add(
        AuditLog(
            ts=_now_ts(),
            payment_id=payment.payment_id,
            rule_id="",
            action="ingest",
            reason=f"webhook payment.failed ({entity.error.reason or 'unknown_reason'})",
            message_source="system",
            outcome="recorded",
            detail={"method": payment.method, "amount_paise": payment.amount_paise},
        )
    )
    return payment, True


def _now_ts() -> int:
    # Sim clock so demo fast-forward keeps webhook timestamps coherent.
    return now_ts()
