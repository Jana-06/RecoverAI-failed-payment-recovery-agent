"""Phase 3 tests: validator (incl. adversarial outputs), templates, writer fallback.

LLM failure paths are tested via fault injection: `write_message` is
monkeypatched to raise (network/timeout) or return hostile text.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.llm.gemini import GeminiError, GeminiResult, MessageRequest
from app.llm.templates import render_template
from app.llm.validator import validate_message
from app.llm import writer as writer_module
from app.llm.writer import compose_message


def make_request(**overrides) -> MessageRequest:
    base = dict(
        first_name="Aarav",
        amount_paise=500_00,
        language="en",
        merchant_name="Acme",
        link="https://rzp.io/i/testlink1",
        tone="friendly",
    )
    base.update(overrides)
    return MessageRequest(**base)


# --- validator happy paths -------------------------------------------------

def test_valid_english_message_passes() -> None:
    v = validate_message(
        "Hi Aarav, your payment of Rs 500.00 to Acme failed. Complete it here: "
        "https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="en",
        customer_name="Aarav",
    )
    assert v.ok, v.errors


def test_valid_hindi_message_passes() -> None:
    v = validate_message(
        "नमस्ते आरव, आपका Acme को Rs 500.00 का भुगतान पूरा नहीं हुआ। यहाँ पूरा करें: "
        "https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="hi",
        customer_name="आरव",
    )
    assert v.ok, v.errors


def test_valid_tamil_message_passes() -> None:
    v = validate_message(
        "வணக்கம் ஆரவ், Acme க்கான Rs 500.00 கட்டணம் செலுத்தப்படவில்லை. இங்கே முடிக்கலாம்: "
        "https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="ta",
        customer_name="ஆரவ்",
    )
    assert v.ok, v.errors


# --- adversarial outputs -----------------------------------------------------

def test_rejects_changed_amount() -> None:
    v = validate_message(
        "Hi Aarav, your payment of Rs 499.00 to Acme failed. Pay: https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="en",
        customer_name="Aarav",
    )
    assert not v.ok
    assert any("amount" in e for e in v.errors)


def test_rejects_rounded_amount_without_paise() -> None:
    # "Rs 500" instead of "Rs 500.00" — amount tampering by laziness.
    v = validate_message(
        "Hi Aarav, your payment of Rs 500 to Acme failed. Pay: https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="en",
        customer_name="Aarav",
    )
    assert not v.ok


def test_rejects_substituted_link() -> None:
    v = validate_message(
        "Hi Aarav, your payment of Rs 500.00 to Acme failed. Pay: https://evil.example/pay",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="en",
        customer_name="Aarav",
    )
    assert not v.ok
    assert any("link" in e or "foreign_url" in e for e in v.errors)


def test_rejects_too_long_message() -> None:
    filler = "please " * 60
    v = validate_message(
        f"Hi Aarav, your payment of Rs 500.00 to Acme failed. {filler} "
        f"https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="en",
        customer_name="Aarav",
    )
    assert not v.ok
    assert any("too_long" in e for e in v.errors)


def test_rejects_hindi_message_without_devanagari() -> None:
    v = validate_message(
        "Hi Aarav, your payment of Rs 500.00 to Acme failed. Pay: https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="hi",
        customer_name="Aarav",
    )
    assert not v.ok
    assert any("wrong_script" in e for e in v.errors)


def test_rejects_discount_promise() -> None:
    for phrase in (
        "Use code SAVE10 for 10% discount",
        "Get 20% cashback when you pay",
        "We will waive the late fee",
        "छूट कूट उपयोग करें",
    ):
        v = validate_message(
            f"Hi Aarav, your payment of Rs 500.00 to Acme failed. {phrase} "
            f"https://rzp.io/i/testlink1",
            amount_paise=500_00,
            link="https://rzp.io/i/testlink1",
            language="en",
            customer_name="Aarav",
        )
        assert not v.ok, phrase
        assert "discount_or_refund_promise" in v.errors


def test_rejects_urgency_threat() -> None:
    for phrase in (
        "Pay today only or your account will be blocked",
        "Last chance! Final notice before legal action",
        "Hurry, link expires tomorrow",
    ):
        v = validate_message(
            f"Hi Aarav, your payment of Rs 500.00 to Acme failed. {phrase} "
            f"https://rzp.io/i/testlink1",
            amount_paise=500_00,
            link="https://rzp.io/i/testlink1",
            language="en",
            customer_name="Aarav",
        )
        assert not v.ok, phrase
        assert "urgency_threat" in v.errors


def test_rejects_leaked_phone_number() -> None:
    v = validate_message(
        "Hi Aarav (+919876543210), your payment of Rs 500.00 to Acme failed. "
        "Pay: https://rzp.io/i/testlink1",
        amount_paise=500_00,
        link="https://rzp.io/i/testlink1",
        language="en",
        customer_name="Aarav",
    )
    assert not v.ok
    assert "phone_number_in_message" in v.errors


# --- templates ---------------------------------------------------------------

@pytest.mark.parametrize("lang", ["en", "hi", "ta"])
def test_templates_always_pass_validator(lang: str) -> None:
    for amount in (500_00, 123_45_678, 149_00):
        req = make_request(
            language=lang,
            amount_paise=amount,
            link="https://rzp.io/i/tpl1",
            mention_alternate_methods=True,
        )
        text = render_template(req)
        v = validate_message(
            text,
            amount_paise=req.amount_paise,
            link=req.link,
            language=lang,
            customer_name=req.first_name,
        )
        assert v.ok, (lang, amount, v.errors, text)


def test_template_falls_back_to_english_for_unknown_language() -> None:
    text = render_template(make_request(language="fr"))
    assert "Hi" in text


# --- writer: source selection + fallback fault injection ----------------------

def test_writer_uses_template_when_no_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "")
    outcome = compose_message(make_request())
    assert outcome.source == "template"
    assert outcome.fallback_used is False  # never tried -> not a fallback
    assert outcome.text  # sendable message exists


def test_writer_falls_back_on_llm_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")

    def boom(req):
        raise GeminiError("timeout after 6s")

    monkeypatch.setattr(writer_module, "write_message", boom)
    outcome = compose_message(make_request())
    assert outcome.source == "template"
    assert outcome.fallback_used is True
    assert "llm_error" in outcome.fallback_reason


def test_writer_falls_back_on_hostile_llm_output(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")

    def hostile(req):
        return GeminiResult(
            text="URGENT! Pay Rs 1.00 now for 50% cashback: http://evil.example",
            latency_ms=250,
            input_tokens=42,
            output_tokens=20,
        )

    monkeypatch.setattr(writer_module, "write_message", hostile)
    outcome = compose_message(make_request())
    assert outcome.source == "template"
    assert outcome.fallback_used is True
    assert "validation_failed" in outcome.fallback_reason
    assert outcome.validation_errors  # violations recorded for the audit log
    assert outcome.latency_ms == 250  # LLM metrics still logged


def test_writer_uses_valid_llm_output(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "fake-key")

    def good(req):
        return GeminiResult(
            text=(
                "Hi Aarav, your payment of Rs 500.00 to Acme didn't go through. "
                "Complete it here: https://rzp.io/i/testlink1"
            ),
            latency_ms=311,
            input_tokens=50,
            output_tokens=28,
        )

    monkeypatch.setattr(writer_module, "write_message", good)
    outcome = compose_message(make_request())
    assert outcome.source == "llm"
    assert outcome.fallback_used is False
    assert outcome.latency_ms == 311
    assert outcome.input_tokens == 50
    assert outcome.output_tokens == 28


def test_message_outcome_detail_includes_metrics() -> None:
    outcome = compose_message(make_request())
    detail = outcome.to_detail()
    assert detail["message_source"] in ("llm", "template")
    assert isinstance(detail["fallback_used"], bool)
    assert "llm_latency_ms" in detail and "llm_output_tokens" in detail
