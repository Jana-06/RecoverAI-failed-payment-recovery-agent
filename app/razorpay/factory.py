"""Choose the Razorpay client at startup.

- Real test-mode keys set -> RealRazorpayClient (network, test mode only).
- Otherwise -> SimulatedRazorpayClient (default, in-memory, deterministic).
"""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.razorpay.base import RazorpayClient
from app.razorpay.simulated import SimulatedRazorpayClient


@lru_cache(maxsize=1)
def get_razorpay_client() -> RazorpayClient:
    if settings.razorpay_key_id and settings.razorpay_key_secret:
        # Import lazily so the demo never needs httpx wired up unnecessarily.
        from app.razorpay.real import RealRazorpayClient

        return RealRazorpayClient(settings.razorpay_key_id, settings.razorpay_key_secret)
    return SimulatedRazorpayClient()
