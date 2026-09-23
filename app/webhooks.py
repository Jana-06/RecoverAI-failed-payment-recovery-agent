"""Razorpay webhook payload models + signature verification.

Razorpay signs webhook bodies with HMAC-SHA256 using the webhook secret;
the signature arrives in the `X-Razorpay-Signature` header (hex digest over
the RAW request body). Docs: https://razorpay.com/docs/webhooks/validate/
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from pydantic import BaseModel, Field


class WebhookError(BaseModel):
    code: str = ""
    description: str = ""
    source: str = ""
    step: str = ""
    reason: str = ""


class WebhookPaymentEntity(BaseModel):
    """Subset of payment entity fields we read from the webhook.

    Extra fields sent by Razorpay are ignored (pydantic default).
    """

    id: str = ""
    order_id: str = ""
    amount: int = 0  # paise
    currency: str = "INR"
    status: str = "failed"
    method: str = ""
    error: WebhookError = Field(default_factory=WebhookError)
    vpa: str | None = None
    card: dict[str, Any] | None = None
    created_at: int = 0
    customer_name: str = ""
    customer_contact: str = ""
    customer_email: str = ""
    notes: dict[str, Any] = Field(default_factory=dict)


class PaymentFailedWebhook(BaseModel):
    """{event: "payment.failed", payload: {payment: {entity: {...}}}}"""

    event: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    def payment_entity(self) -> WebhookPaymentEntity:
        raw = (self.payload.get("payment") or {}).get("entity") or {}
        return WebhookPaymentEntity(**raw)


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check of the raw body vs X-Razorpay-Signature."""
    if not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign_webhook_body(raw_body: bytes, secret: str) -> str:
    """Helper for tests/demo: produce a valid signature for a body."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def parse_event(raw_body: bytes) -> PaymentFailedWebhook:
    """Parse + validate the JSON body (raises pydantic ValidationError on junk)."""
    data = json.loads(raw_body.decode("utf-8"))
    return PaymentFailedWebhook(**data)
