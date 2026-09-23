"""Recovery-probability lookup table (documented assumptions).

ESTIMATES used only for prioritization and simulated outcomes. These are
assumptions informed by public postmortems on payment recovery, not measured
values — the demo labels them as simulated everywhere they surface.

p_24h = probability of recovery within 24h of a fresh contact/retry.
p_7d  = probability of recovery within 7 days (used for wait-strategy value).

Structure: reason -> {method: (p_24h, p_7d)}.
Rationale (documented assumptions):
- timeouts recover best on retry (transient);
- insufficient_funds recovers after payday — decent 7d, low 24h;
- auth failures recover when the customer re-enters OTP/3DS;
- risk categories are near-zero by construction (never auto-recovered);
- wallets/UPI (stored credentials) recover easier than netbanking.
"""

from __future__ import annotations

# reason -> {method: (p_24h, p_7d)}
RECOVERY_PROBABILITY: dict[str, dict[str, tuple[float, float]]] = {
    "timeout": {
        "upi": (0.55, 0.68),
        "card": (0.45, 0.58),
        "netbanking": (0.35, 0.48),
        "wallet": (0.50, 0.62),
        "*": (0.40, 0.52),
    },
    "bank_technical_error": {
        "upi": (0.50, 0.62),
        "card": (0.40, 0.52),
        "netbanking": (0.30, 0.42),
        "wallet": (0.45, 0.55),
        "*": (0.35, 0.45),
    },
    "network_error": {
        "upi": (0.48, 0.60),
        "card": (0.38, 0.50),
        "netbanking": (0.28, 0.40),
        "wallet": (0.42, 0.54),
        "*": (0.34, 0.44),
    },
    "insufficient_funds": {
        "upi": (0.18, 0.52),
        "card": (0.12, 0.40),
        "netbanking": (0.10, 0.35),
        "wallet": (0.20, 0.50),
        "*": (0.15, 0.45),
    },
    "authentication_failed": {
        "upi": (0.40, 0.55),
        "card": (0.35, 0.50),
        "netbanking": (0.25, 0.38),
        "wallet": (0.38, 0.50),
        "*": (0.30, 0.42),
    },
    "customer_cancelled": {
        "upi": (0.25, 0.45),
        "card": (0.20, 0.40),
        "netbanking": (0.15, 0.30),
        "wallet": (0.22, 0.40),
        "*": (0.18, 0.35),
    },
    # Risk categories: never auto-recovered; table exists so scoring stays total.
    "card_blocked_or_fraud": {"*": (0.0, 0.0)},
    "unknown": {"*": (0.0, 0.0)},
}

# Falloff applied per prior recovery attempt (customers ignore nudges).
ATTEMPT_FALLOFF = 0.6  # p *= 0.6 ** prior_attempts

# Classification can produce more specific reason codes than the canonical
# table keys; alias them to the closest canonical entry so lookups never
# silently fall through to the (near-zero) unknown row.
REASON_ALIASES: dict[str, str] = {
    "otp_entered_incorrectly": "authentication_failed",
    "wrong_otp": "authentication_failed",
    "3ds_failure": "authentication_failed",
    "three_ds_failure": "authentication_failed",
    "authentication_intent_failed": "authentication_failed",
    "payment_cancelled_by_user": "customer_cancelled",
    "dropout": "customer_cancelled",
    "balance_insufficient": "insufficient_funds",
    "gateway_error": "timeout",
    "payment_processing_error": "timeout",
    "issuer_server_unavailable": "bank_technical_error",
    "card_blocked": "card_blocked_or_fraud",
    "fraud_suspected": "card_blocked_or_fraud",
    "high_risk_customer": "card_blocked_or_fraud",
    "blacklisted_card": "card_blocked_or_fraud",
}


def _canonical_reason(reason: str) -> str:
    if reason in RECOVERY_PROBABILITY:
        return reason
    return REASON_ALIASES.get(reason, "unknown")


def get_probability(reason: str, method: str, prior_attempts: int = 0) -> tuple[float, float]:
    """(p_24h, p_7d) with alias/wildcard fallback and attempt falloff, clamped [0,1]."""
    table = RECOVERY_PROBABILITY.get(_canonical_reason(reason), RECOVERY_PROBABILITY["unknown"])
    pair = table.get(method, table["*"])
    p24, p7 = pair
    factor = ATTEMPT_FALLOFF ** max(0, prior_attempts)
    return (min(1.0, p24 * factor), min(1.0, p7 * factor))
