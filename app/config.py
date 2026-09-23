"""Environment-driven configuration with safe defaults.

The demo must run with ZERO configuration: no LLM key, no Razorpay keys.
Anything sensitive or network-touching is opt-in via env vars.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """Immutable-ish settings container (read once at import)."""

    def __init__(self) -> None:
        # --- LLM (message writer only) ---
        self.gemini_api_key: str = _env_str("GEMINI_API_KEY", "").strip()
        self.gemini_model: str = _env_str("GEMINI_MODEL", "gemini-2.0-flash")

        # --- Razorpay ---
        self.razorpay_key_id: str = _env_str("RAZORPAY_KEY_ID", "").strip()
        self.razorpay_key_secret: str = _env_str("RAZORPAY_KEY_SECRET", "").strip()
        self.razorpay_webhook_secret: str = _env_str(
            "RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret"
        )

        # --- Storage ---
        self.db_url: str = _env_str("RECOVERAI_DB", "data/recoverai.db")

        # --- Guardrail knobs ---
        self.daily_message_budget: int = _env_int("DAILY_MESSAGE_BUDGET", 200)
        self.high_value_paise: int = _env_int("HIGH_VALUE_PAISE", 2_500_000)

        # --- Demo chaos engineering ---
        self.chaos: bool = _env_str("CHAOS", "0").strip() in ("1", "true", "yes")


settings = Settings()
