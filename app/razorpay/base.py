"""RazorpayClient interface.

Both the simulated (default, no network) and real (test-mode) clients
implement this. The executor depends only on this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.razorpay.types import CreateLinkResult, FetchPaymentResult, RazorpayCustomer


class RazorpayClient(ABC):
    """Interface for the two Razorpay operations RecoverAI needs."""

    @abstractmethod
    def create_payment_link(
        self,
        amount: int,
        customer: RazorpayCustomer,
        description: str = "",
        reference_id: str = "",
        expire_seconds: int = 7 * 24 * 3600,
    ) -> CreateLinkResult:
        """Create a Razorpay Payment Link for `amount` paise.

        Never raises for business failures; returns ok=False + error string
        so the executor can log and continue.
        """

    @abstractmethod
    def fetch_payment(self, payment_id: str) -> FetchPaymentResult:
        """Fetch a payment by id to confirm its (possibly updated) status."""
