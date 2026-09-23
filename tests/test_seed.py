"""Tests for the seeded synthetic dataset (shape, determinism, edge cases)."""

from __future__ import annotations

from collections import Counter

from app.seed import DAYS, TOTAL_FAILURES, generate_payments


def test_dataset_size_and_window() -> None:
    rows = generate_payments()
    assert len(rows) == TOTAL_FAILURES
    assert TOTAL_FAILURES == 300

    span_seconds = max(r["ts"] for r in rows) - min(r["ts"] for r in rows)
    assert span_seconds <= DAYS * 24 * 3600 + 60


def test_dataset_is_deterministic() -> None:
    a = generate_payments()
    b = generate_payments()
    assert [p["payment_id"] for p in a] == [p["payment_id"] for p in b]
    assert [p["amount"] for p in a] == [p["amount"] for p in b]
    assert [(p["spec"].reason, p["ts"]) for p in a] == [(p["spec"].reason, p["ts"]) for p in b]


def test_failure_mix_is_realistic() -> None:
    rows = generate_payments()
    reasons = Counter(r["spec"].reason for r in rows)
    # UPI/network timeouts dominate (mirrors real merchant distributions).
    assert reasons["timeout"] > reasons["customer_cancelled"]
    assert reasons["timeout"] > reasons["card_blocked_or_fraud"]
    # Every documented reason is present.
    for reason in (
        "timeout",
        "bank_technical_error",
        "insufficient_funds",
        "authentication_failed",
        "customer_cancelled",
        "card_blocked_or_fraud",
        "network_error",
    ):
        assert reasons[reason] > 0, f"missing {reason} in seeded mix"


def test_methods_are_valid() -> None:
    rows = generate_payments()
    assert {r["method"] for r in rows} <= {"upi", "card", "netbanking", "wallet"}


def test_edge_cases_present() -> None:
    rows = generate_payments()
    edges = Counter(r["edge"] for r in rows)
    assert edges["opted_out"] >= 2
    assert edges["very_high_value"] >= 3
    assert edges["already_recovered"] == 3
    assert edges["duplicate"] >= 2


def test_high_value_payments_exceed_approval_threshold() -> None:
    from app.config import settings

    rows = [r for r in generate_payments() if r["edge"] == "very_high_value"]
    assert all(r["amount"] >= settings.high_value_paise for r in rows)


def test_all_amounts_are_int_paise() -> None:
    rows = generate_payments()
    assert all(isinstance(r["amount"], int) and r["amount"] > 0 for r in rows)


def test_customers_have_valid_languages_and_masked_phones() -> None:
    rows = generate_payments()
    langs = {r["customer"].language for r in rows}
    assert langs <= {"en", "hi", "ta"}
    for r in rows:
        assert r["customer"].masked_phone.startswith("*")
        assert len(r["customer"].masked_phone) == 10
