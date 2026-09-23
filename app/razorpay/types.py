"""Typed models for Razorpay-style objects and client results.

Field names and shapes mirror Razorpay's public API where it matters:
- amounts are integers in **paise**
- payment.failed webhook: {event, payload:{payment:{entity:{...}}}}
- payment links: {id, short_url, amount, currency, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RazorpayCustomer:
    """Customer as it appears in Razorpay payment link / payment payloads."""

    name: str = ""
    contact: str = ""  # phone, E.164-ish, e.g. "+919876543210"
    email: str = ""


@dataclass
class PaymentEntity:
    """Subset of Razorpay's payment entity that RecoverAI cares about.

    Mirrors the `payload.payment.entity` object of a payment.failed webhook.
    """

    id: str  # e.g. "pay_Qm9xYzAbCdEf"
    order_id: str  # e.g. "order_Qm9xYzAbCdEf"
    amount: int  # paise
    currency: str = "INR"
    status: str = "failed"  # Razorpay status: created|authorized|captured|failed|refunded
    method: str = ""  # upi|card|netbanking|wallet|emi|...
    error_code: str = ""  # e.g. BAD_REQUEST_ERROR / GATEWAY_ERROR
    error_description: str = ""  # e.g. "payment cancelled by user"
    error_source: str = ""  # customer|gateway|bank|network|internal
    error_step: str = ""  # payment_initiation|authorization|authentication
    error_reason: str = ""  # machine-readable: insufficient_funds, timeout, ...
    vpa: str | None = None  # UPI handle, masked downstream if persisted
    card_last4: str | None = None
    created_at: int = 0  # epoch seconds
    customer: RazorpayCustomer = field(default_factory=RazorpayCustomer)


@dataclass
class PaymentLink:
    """Subset of Razorpay Payment Links API response."""

    id: str  # e.g. "plink_Qm9xYzAbCdEf"
    short_url: str
    amount: int  # paise
    currency: str = "INR"
    reference_id: str = ""
    status: str = "created"  # created|partially_paid|paid|cancelled|expired
    customer: RazorpayCustomer = field(default_factory=RazorpayCustomer)


@dataclass
class FetchPaymentResult:
    """Result of fetch_payment(): the entity plus whether the call succeeded."""

    ok: bool
    payment: PaymentEntity | None = None
    error: str = ""


@dataclass
class CreateLinkResult:
    """Result of create_payment_link(): link plus success flag.

    ok=False means the call failed (chaos, network, upstream) — the executor
    treats this as `error` outcome and logs it; it never swallows silently.
    """

    ok: bool
    link: PaymentLink | None = None
    error: str = ""


def entity_from_webhook(entity: dict[str, Any]) -> PaymentEntity:
    """Normalize a webhook `payload.payment.entity` dict into PaymentEntity.

    Tolerant of missing keys (Razorpay adds fields over time).
    """
    cust = entity.get("customer") or {}
    notes = entity.get("notes") or {}
    return PaymentEntity(
        id=str(entity.get("id", "")),
        order_id=str(entity.get("order_id", "") or notes.get("order_id", "")),
        amount=int(entity.get("amount", 0)),
        currency=str(entity.get("currency", "INR")),
        status=str(entity.get("status", "failed")),
        method=str(entity.get("method", "") or ""),
        error_code=str(entity.get("error_code", "") or ""),
        error_description=str(entity.get("error_description", "") or ""),
        error_source=str(entity.get("error_source", "") or ""),
        error_step=str(entity.get("error_step", "") or ""),
        error_reason=str(entity.get("error_reason", "") or ""),
        card_last4=(str(entity.get("card", {}).get("last4", "")) or None)
        if isinstance(entity.get("card"), dict)
        else None,
        created_at=int(entity.get("created_at", 0)),
        customer=RazorpayCustomer(
            name=str(cust.get("name", "") or notes.get("customer_name", "")),
            contact=str(cust.get("contact", "") or notes.get("customer_contact", "")),
            email=str(cust.get("email", "") or ""),
        ),
    )
