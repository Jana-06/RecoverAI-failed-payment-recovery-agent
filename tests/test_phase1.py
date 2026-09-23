"""Phase 1 unit tests: PII masking, INR formatting, clock, Razorpay clients."""

from __future__ import annotations

import time

import pytest

from app.clock import advance, now_ts, offset_seconds, reset
from app.utils import format_inr, ist_hour, mask_phone


@pytest.fixture(autouse=True)
def _reset_clock():
    reset()
    yield
    reset()


# --- utils ----------------------------------------------------------

def test_mask_phone_keeps_last_four() -> None:
    assert mask_phone("+919876543210") == "******3210"
    assert mask_phone("98765") == "*8765"
    assert mask_phone("") == ""
    assert mask_phone(None) == ""


def test_format_inr_uses_indian_grouping() -> None:
    assert format_inr(12_34_56_700) == "Rs 12,34,567.00"
    assert format_inr(100) == "Rs 1.00"
    assert format_inr(50_000) == "Rs 500.00"
    assert format_inr(2_500_000) == "Rs 25,000.00"


def test_ist_hour_converts_utc_to_ist() -> None:
    # 2026-09-15 20:30 UTC == 02:00 IST next day.
    ts = 1757970600
    assert ist_hour(ts) == 2


# --- sim clock --------------------------------------------------------

def test_clock_starts_at_real_time() -> None:
    assert abs(now_ts() - int(time.time())) < 5
    assert offset_seconds() == 0.0


def test_clock_fast_forward_moves_now() -> None:
    before = now_ts()
    new_now = advance(24 * 3600)
    assert new_now >= before + 24 * 3600 - 2
    assert offset_seconds() == 24 * 3600
    assert now_ts() >= before + 24 * 3600 - 2


def test_clock_cannot_rewind() -> None:
    with pytest.raises(ValueError):
        advance(-60)


# --- razorpay clients ---------------------------------------------------

def test_simulated_client_creates_deterministic_links() -> None:
    from app.razorpay.simulated import SimulatedRazorpayClient
    from app.razorpay.types import RazorpayCustomer

    c1 = SimulatedRazorpayClient(seed=7)
    c2 = SimulatedRazorpayClient(seed=7)
    r1 = c1.create_payment_link(50_000, RazorpayCustomer(name="A"))
    r2 = c2.create_payment_link(50_000, RazorpayCustomer(name="A"))
    assert r1.ok and r2.ok
    assert r1.link.id == r2.link.id  # same seed -> same id stream
    assert r1.link.amount == 50_000
    assert r1.link.short_url.startswith("https://rzp.io/i/")


def test_simulated_client_rejects_nonpositive_amount() -> None:
    from app.razorpay.simulated import SimulatedRazorpayClient
    from app.razorpay.types import RazorpayCustomer

    result = SimulatedRazorpayClient().create_payment_link(0, RazorpayCustomer())
    assert not result.ok
    assert result.error == "amount_must_be_positive"


def test_simulated_client_fetches_registered_payment() -> None:
    from app.razorpay.simulated import SimulatedRazorpayClient
    from app.razorpay.types import PaymentEntity

    client = SimulatedRazorpayClient()
    miss = client.fetch_payment("pay_missing")
    assert not miss.ok

    client.register_payment(PaymentEntity(id="pay_x", order_id="order_x", amount=100))
    hit = client.fetch_payment("pay_x")
    assert hit.ok and hit.payment.amount == 100


def test_factory_defaults_to_simulated_without_keys(monkeypatch) -> None:
    from app.razorpay import factory
    from app.razorpay.simulated import SimulatedRazorpayClient

    monkeypatch.setattr(factory.settings, "razorpay_key_id", "")
    monkeypatch.setattr(factory.settings, "razorpay_key_secret", "")
    assert isinstance(factory.get_razorpay_client(), SimulatedRazorpayClient)


def test_real_client_requires_keys() -> None:
    from app.razorpay.real import RealRazorpayClient

    with pytest.raises(ValueError):
        RealRazorpayClient("", "")


def test_webhook_payload_models_shape() -> None:
    from app.webhooks import PaymentFailedWebhook

    body = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_1",
                    "amount": 1234,  # paise
                    "error": {"reason": "insufficient_funds", "source": "bank"},
                }
            }
        },
    }
    hook = PaymentFailedWebhook(**body)
    ent = hook.payment_entity()
    assert ent.id == "pay_1"
    assert ent.amount == 1234
    assert ent.error.reason == "insufficient_funds"
