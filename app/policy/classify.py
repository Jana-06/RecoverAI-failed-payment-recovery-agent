"""Failure classification: Razorpay error signals -> policy category.

A category is what the RULES act on; the raw reason is what Razorpay sent.
Mapping is intentionally explicit (no fuzzy matching on hot paths) with a
defensive substring pass over the description for unknown codes — misrouted
risk-category failures would auto-recover fraud, so unknown reasons are
classified conservatively as `review` rather than guessed as recoverable.
"""

from __future__ import annotations

from dataclasses import dataclass


# The four categories the rules engine understands.
CATEGORY_TECHNICAL = "technical"        # transient, retrying same method usually works
CATEGORY_INSUFFICIENT_FUNDS = "insufficient_funds"
CATEGORY_CUSTOMER_ACTION = "customer_action"  # customer can fix by retrying/OTP
CATEGORY_RISK = "risk"                  # fraud/blocked: never auto-recover
CATEGORY_UNKNOWN = "unknown"            # not confident -> human review

REVIEWABLE = (CATEGORY_RISK, CATEGORY_UNKNOWN)


@dataclass(frozen=True)
class Classification:
    category: str
    reason: str          # normalized reason used downstream
    signal: str          # which field matched (for explainability)


# Canonical reason -> category. Keys are normalized (lowercase, {, -> _).
_REASON_CATEGORY: dict[str, str] = {
    # transient / technical
    "timeout": CATEGORY_TECHNICAL,
    "bank_technical_error": CATEGORY_TECHNICAL,
    "network_error": CATEGORY_TECHNICAL,
    "gateway_error": CATEGORY_TECHNICAL,
    "payment_processing_error": CATEGORY_TECHNICAL,
    "issuer_server_unavailable": CATEGORY_TECHNICAL,
    # money availability
    "insufficient_funds": CATEGORY_INSUFFICIENT_FUNDS,
    "balance_insufficient": CATEGORY_INSUFFICIENT_FUNDS,
    # customer-action (retry with attention usually succeeds)
    "authentication_failed": CATEGORY_CUSTOMER_ACTION,
    "authentication_intent_failed": CATEGORY_CUSTOMER_ACTION,
    "otp_entered_incorrectly": CATEGORY_CUSTOMER_ACTION,
    "wrong_otp": CATEGORY_CUSTOMER_ACTION,
    "3ds_failure": CATEGORY_CUSTOMER_ACTION,
    "three_ds_failure": CATEGORY_CUSTOMER_ACTION,
    "customer_cancelled": CATEGORY_CUSTOMER_ACTION,
    "payment_cancelled_by_user": CATEGORY_CUSTOMER_ACTION,
    "dropout": CATEGORY_CUSTOMER_ACTION,
    # risk / blocked — NEVER auto-recover
    "card_blocked_or_fraud": CATEGORY_RISK,
    "card_blocked": CATEGORY_RISK,
    "suspected_fraud": CATEGORY_RISK,
    "fraud_suspected": CATEGORY_RISK,
    "high_risk_customer": CATEGORY_RISK,
    "blacklisted_card": CATEGORY_RISK,
}

# Substring -> category, applied to the description only when the code lookup
# misses. Ordered: risk patterns checked FIRST (conservative routing).
_DESCRIPTION_PATTERNS: list[tuple[str, str]] = [
    ("fraud", CATEGORY_RISK),
    ("blocked", CATEGORY_RISK),
    ("blacklist", CATEGORY_RISK),
    ("stolen", CATEGORY_RISK),
    ("insufficient", CATEGORY_INSUFFICIENT_FUNDS),
    ("no balance", CATEGORY_INSUFFICIENT_FUNDS),
    ("cancel", CATEGORY_CUSTOMER_ACTION),
    ("otp", CATEGORY_CUSTOMER_ACTION),
    ("authentication", CATEGORY_CUSTOMER_ACTION),
    ("3ds", CATEGORY_CUSTOMER_ACTION),
    ("timeout", CATEGORY_TECHNICAL),
    ("timed out", CATEGORY_TECHNICAL),
    ("unavailable", CATEGORY_TECHNICAL),
    ("server", CATEGORY_TECHNICAL),
]


def _normalize(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "_").replace("-", "_")


def classify(
    error_reason: str,
    error_description: str = "",
    error_source: str = "",
    error_step: str = "",
) -> Classification:
    """Classify a failed payment into exactly one policy category.

    Priority: explicit reason code > source/step hints > description patterns
    > conservative `unknown`.
    """
    reason = _normalize(error_reason)

    hit = _REASON_CATEGORY.get(reason)
    if hit:
        return Classification(category=hit, reason=reason, signal="error_reason")

    # Bank/customer source + auth step hints (no reason code available).
    if _normalize(error_source) == "bank" and _normalize(error_step) == "authentication":
        return Classification(
            category=CATEGORY_CUSTOMER_ACTION, reason=reason or "authentication_failed",
            signal="source_step_hint",
        )

    description = _normalize(error_description)
    if description:
        for pattern, category in _DESCRIPTION_PATTERNS:
            if pattern in description:
                return Classification(
                    category=category, reason=reason or pattern, signal="description"
                )

    # Unknown reasons default to human review — the safe default.
    return Classification(
        category=CATEGORY_UNKNOWN, reason=reason or "unclassified", signal="fallback"
    )
