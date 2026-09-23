"""Gemini REST client (minimal, dependency-free) — writes message copy ONLY.

Design constraints:
- The prompt contains NO phone numbers or PII beyond the first name.
- Hard timeout; any error/timeout raises GeminiError -> template fallback.
- Tracks latency + token counts for logging.

Uses the REST API directly (no SDK) so the demo has zero extra deps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings


class GeminiError(Exception):
    """Any LLM failure: network, timeout, HTTP error, or unusable response."""


@dataclass
class GeminiResult:
    text: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class MessageRequest:
    """Minimal, PII-free inputs. Nothing else reaches the model."""

    first_name: str
    amount_paise: int
    language: str          # en | hi | ta
    merchant_name: str
    link: str
    tone: str = "friendly"
    # R2 variant: insufficient-funds nudges may offer an alternate method.
    mention_alternate_methods: bool = False


def _build_prompt(req: MessageRequest) -> str:
    amount = f"Rs {req.amount_paise / 100:,.2f}"
    # Script requirement is stated explicitly per language.
    script_note = {
        "en": "English (Latin script)",
        "hi": "Hindi (Devanagari script)",
        "ta": "Tamil (Tamil script)",
    }[req.language]
    return (
        "You write a single short payment-request SMS for a failed payment recovery service.\n"
        f"Customer first name: {req.first_name}\n"
        f"Merchant: {req.merchant_name}\n"
        f"Amount due: {amount}\n"
        f"Payment link: {req.link}\n"
        f"Language: {script_note}\n"
        f"Tone: {req.tone}, respectful, no pressure.\n"
        "STRICT RULES:\n"
        "- Include the exact amount string '" + amount + "'.\n"
        "- Include the exact link " + req.link + "\n"
        "- Maximum 300 characters.\n"
        "- NEVER promise discounts, cashback, refunds, waivers or offers.\n"
        "- NEVER use urgency, deadlines, threats or negative consequences.\n"
        "- Do not include phone numbers or any data not listed above.\n"
        "Return ONLY the message text, nothing else."
    )


def write_message(req: MessageRequest, timeout_seconds: float = 6.0) -> GeminiResult:
    """Call Gemini generateContent; raises GeminiError on any failure."""
    api_key = settings.gemini_api_key
    if not api_key:
        raise GeminiError("gemini_not_configured")

    model = settings.gemini_model or "gemini-2.0-flash"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    prompt = _build_prompt(req)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 128,
            "candidateCount": 1,
        },
        # Safer defaults; the validator remains the real gate.
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }

    import time as _time

    started = _time.perf_counter()
    try:
        resp = httpx.post(url, json=payload, timeout=httpx.Timeout(timeout_seconds))
    except httpx.HTTPError as exc:
        raise GeminiError(f"network_error: {exc}") from exc

    latency_ms = int((_time.perf_counter() - started) * 1000)

    if resp.status_code == 429:
        raise GeminiError("rate_limited")
    if resp.status_code >= 400:
        raise GeminiError(f"api_error_{resp.status_code}")

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise GeminiError("invalid_json") from exc

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError("unexpected_response_shape") from exc

    usage = data.get("usageMetadata") or {}
    return GeminiResult(
        text=text,
        latency_ms=latency_ms,
        input_tokens=int(usage.get("promptTokenCount", 0)),
        output_tokens=int(usage.get("candidatesTokenCount", 0)),
    )
