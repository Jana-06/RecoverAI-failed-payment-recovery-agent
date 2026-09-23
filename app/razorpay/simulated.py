"""SimulatedRazorpayClient — in-memory, deterministic, no network.

Default client for the demo. Generates Razorpay-shaped ids (plink_/pay_),
tracks state per instance, and supports CHAOS fault injection (random
failures) so the demo can show graceful degradation.
"""

from __future__ import annotations

import random
import string
import threading

from app.config import settings
from app.razorpay.base import RazorpayClient
from app.razorpay.types import (
    CreateLinkResult,
    FetchPaymentResult,
    PaymentEntity,
    PaymentLink,
    RazorpayCustomer,
)

_ALPHABET = string.ascii_letters + string.digits


def _rzp_id(prefix: str, rng: random.Random) -> str:
    """Razorpay-shaped id, e.g. 'plink_Qm9xYzAbCdEf' (14 chars)."""
    return f"{prefix}_" + "".join(rng.choice(_ALPHABET) for _ in range(14))


class SimulatedRazorpayClient(RazorpayClient):
    """In-memory double for the Razorpay API.

    - create_payment_link: always succeeds unless CHAOS rolls a failure.
    - fetch_payment: returns the payment if it was registered (webhook ingest
      does this), else ok=False with a not-found error.
    """

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._links: dict[str, PaymentLink] = {}
        self._payments: dict[str, PaymentEntity] = {}
        self.chaos_failures = 0

    # -- helpers used by tests/seed/ingest -------------------------------
    def register_payment(self, payment: PaymentEntity) -> None:
        """Make a payment fetchable (simulates Razorpay having the record)."""
        with self._lock:
            self._payments[payment.id] = payment

    # -- RazorpayClient interface ----------------------------------------
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

        if settings.chaos and self._rng.random() < 0.25:
            self.chaos_failures += 1
            return CreateLinkResult(ok=False, error="chaos_upstream_500")

        link_id = _rzp_id("plink", self._rng)
        link = PaymentLink(
            id=link_id,
            short_url=f"https://rzp.io/i/{link_id[6:]}",
            amount=amount,
            currency="INR",
            reference_id=reference_id,
            status="created",
            customer=customer,
        )
        with self._lock:
            self._links[link_id] = link
        return CreateLinkResult(ok=True, link=link)

    def fetch_payment(self, payment_id: str) -> FetchPaymentResult:
        if settings.chaos and self._rng.random() < 0.15:
            self.chaos_failures += 1
            return FetchPaymentResult(ok=False, error="chaos_upstream_timeout")
        with self._lock:
            payment = self._payments.get(payment_id)
        if payment is None:
            return FetchPaymentResult(ok=False, error="payment_not_found")
        return FetchPaymentResult(ok=True, payment=payment)

    # -- introspection for the dashboard/tests ---------------------------
    def links_created(self) -> int:
        with self._lock:
            return len(self._links)
