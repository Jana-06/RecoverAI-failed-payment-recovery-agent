"""RealRazorpayClient — talks to the Razorpay REST API (TEST MODE ONLY).

Used only when RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are set. Deliberately
boring: Basic auth, short timeouts, never raises for upstream errors (returns
ok=False) so the executor can log and continue.

Docs: https://razorpay.com/docs/api/payment-links and /payments
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.razorpay.base import RazorpayClient
from app.razorpay.types import (
    CreateLinkResult,
    FetchPaymentResult,
    RazorpayCustomer,
)

_API_BASE = "https://api.razorpay.com/v1"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class RealRazorpayClient(RazorpayClient):
    def __init__(self, key_id: str, key_secret: str) -> None:
        if not key_id or not key_secret:
            raise ValueError("RealRazorpayClient requires test-mode key id and secret")
        self._auth = (
            "Basic "
            + base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth,
            "Content-Type": "application/json",
        }

    def create_payment_link(
        self,
        amount: int,
        customer: RazorpayCustomer,
        description: str = "",
        reference_id: str = "",
        expire_seconds: int = 7 * 24 * 3600,
    ) -> CreateLinkResult:
        if amount <= 0:
            return CreateLinkResult(ok=False, error="amount_must_be_positive")
        import time as _time

        payload: dict[str, Any] = {
            "amount": amount,
            "currency": "INR",
            "accept_partial": False,
            "expire_by": int(_time.time()) + expire_seconds,
            "reference_id": reference_id or None,
            "description": description or "Payment retry",
            "customer": {
                "name": customer.name or "Customer",
                "contact": customer.contact,
                "email": customer.email or None,
            },
            "notify": {"sms": bool(customer.contact), "email": False},
            "reminder_enable": False,  # RecoverAI owns reminders, not Razorpay
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            resp = httpx.post(
                f"{_API_BASE}/payment_links",
                json=payload,
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return CreateLinkResult(ok=False, error=f"network_error: {exc}")
        if resp.status_code >= 400:
            return CreateLinkResult(
                ok=False, error=f"razorpay_error_{resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        return CreateLinkResult(
            ok=True,
            link=self._link_from_api(data),
        )

    def fetch_payment(self, payment_id: str) -> FetchPaymentResult:
        try:
            resp = httpx.get(
                f"{_API_BASE}/payments/{payment_id}",
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return FetchPaymentResult(ok=False, error=f"network_error: {exc}")
        if resp.status_code >= 400:
            return FetchPaymentResult(
                ok=False, error=f"razorpay_error_{resp.status_code}"
            )
        data = resp.json()
        return FetchPaymentResult(ok=True, payment=self._entity_from_api(data))

    # -- API dict -> typed models -----------------------------------------
    def _link_from_api(self, data: dict[str, Any]):
        from app.razorpay.types import PaymentLink

        return PaymentLink(
            id=str(data.get("id", "")),
            short_url=str(data.get("short_url", "")),
            amount=int(data.get("amount", 0)),
            currency=str(data.get("currency", "INR")),
            reference_id=str(data.get("reference_id", "") or ""),
            status=str(data.get("status", "created")),
            customer=RazorpayCustomer(
                name=str((data.get("customer") or {}).get("name", "")),
                contact=str((data.get("customer") or {}).get("contact", "")),
                email=str((data.get("customer") or {}).get("email", "")),
            ),
        )

    def _entity_from_api(self, data: dict[str, Any]):
        from app.razorpay.types import PaymentEntity, RazorpayCustomer

        return PaymentEntity(
            id=str(data.get("id", "")),
            order_id=str(data.get("order_id", "") or ""),
            amount=int(data.get("amount", 0)),
            currency=str(data.get("currency", "INR")),
            status=str(data.get("status", "")),
            method=str(data.get("method", "") or ""),
            error_code=str(data.get("error_code", "") or ""),
            error_description=str(data.get("error_description", "") or ""),
            error_source=str(data.get("error_source", "") or ""),
            error_step=str(data.get("error_step", "") or ""),
            error_reason=str(data.get("error_reason", "") or ""),
            created_at=int(data.get("created_at", 0)),
            customer=RazorpayCustomer(
                name=str((data.get("customer") or {}).get("name", "")),
                contact=str((data.get("customer") or {}).get("contact", "")),
                email=str((data.get("customer") or {}).get("email", "")),
            ),
        )
