"""Writer facade: LLM first (if configured), validator as the gate, template as the floor.

Contract (per buildathon spec):
- LLM gets MINIMAL inputs: first name, amount, language, merchant, link, tone.
- Output is validated (amount/link/script/length/no promises/no threats).
- On validation failure, LLM error, or LLM timeout -> template fallback with
  `fallback_used=true`.
- Latency + token usage + fallback flag are always recorded for the audit log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import settings
from app.llm.gemini import GeminiError, MessageRequest, write_message
from app.llm.templates import render_template
from app.llm.validator import validate_message

logger = logging.getLogger("recoverai.writer")


@dataclass
class MessageOutcome:
    text: str
    source: str                 # "llm" | "template"
    fallback_used: bool
    fallback_reason: str = ""   # "" | validator errors joined | gemini error
    latency_ms: int = 0         # LLM latency (0 for pure template)
    input_tokens: int = 0
    output_tokens: int = 0
    validation_errors: list[str] = field(default_factory=list)

    def to_detail(self) -> dict:
        return {
            "message_source": self.source,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "llm_latency_ms": self.latency_ms,
            "llm_input_tokens": self.input_tokens,
            "llm_output_tokens": self.output_tokens,
            "validation_errors": self.validation_errors,
        }


def compose_message(req: MessageRequest) -> MessageOutcome:
    """Compose a validated customer message. NEVER raises.

    Guarantees a sendable message: either validated LLM output or the
    template. The returned text is always validator-approved when a link is
    present (the caller supplies the link and passes it back for validation).
    """
    # No key configured -> straight to template (the default demo path).
    if not settings.gemini_api_key:
        text = render_template(req)
        # Belt-and-braces: even templates are validated.
        validation = validate_message(
            text,
            amount_paise=req.amount_paise,
            link=req.link,
            language=req.language,
            customer_name=req.first_name,
        )
        return MessageOutcome(
            text=text,
            source="template",
            fallback_used=False,  # not a *fallback*; LLM was never tried
            validation_errors=validation.errors,
        )

    # Try the LLM. ANY failure — GeminiError, SDK bugs, unexpected exceptions —
    # must fall back to the template: compose_message NEVER raises.
    try:
        result = write_message(req)
    except Exception as exc:  # noqa: BLE001 - the floor must hold for anything
        text = render_template(req)
        return MessageOutcome(
            text=text,
            source="template",
            fallback_used=True,
            fallback_reason=f"llm_error: {type(exc).__name__}: {exc}",
        )

    validation = validate_message(
        result.text,
        amount_paise=req.amount_paise,
        link=req.link,
        language=req.language,
        customer_name=req.first_name,
    )
    if validation.ok:
        return MessageOutcome(
            text=result.text,
            source="llm",
            fallback_used=False,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    # LLM answered but violated the contract -> template, and record why.
    logger.warning(
        "LLM message rejected by validator: %s", validation.errors
    )
    text = render_template(req)
    return MessageOutcome(
        text=text,
        source="template",
        fallback_used=True,
        fallback_reason="validation_failed: " + "; ".join(validation.errors),
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        validation_errors=validation.errors,
    )
