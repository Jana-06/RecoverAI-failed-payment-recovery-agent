"""Template fallback messages — the guaranteed floor.

These fire when the LLM is unconfigured, errors, times out, or produces
output the validator rejects. Templates are per-language and deliberately
boring: exact amount, exact link, friendly, no promises. They must ALWAYS
pass the validator (there is a test enforcing that).
"""

from __future__ import annotations

from app.llm.gemini import MessageRequest
from app.utils import format_inr

# {first_name}, {amount}, {link}, {merchant}
_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "standard": (
            "Hi {first_name}, your payment of {amount} to {merchant} didn't go "
            "through. You can complete it securely here: {link}"
        ),
        "alternate": (
            "Hi {first_name}, your payment of {amount} to {merchant} didn't go "
            "through. You can pay via UPI, card or netbanking here: {link}"
        ),
    },
    "hi": {
        "standard": (
            "नमस्ते {first_name}, आपका {merchant} को {amount} का भुगतान पूरा नहीं हुआ। "
            "इसे सुरक्षित रूप से यहाँ पूरा करें: {link}"
        ),
        "alternate": (
            "नमस्ते {first_name}, आपका {merchant} को {amount} का भुगतान पूरा नहीं हुआ। "
            "UPI, कार्ड या नेटबैंकिंग से यहाँ भुगतान करें: {link}"
        ),
    },
    "ta": {
        "standard": (
            "வணக்கம் {first_name}, {merchant} நிறுவனத்திற்கான உங்கள் {amount} "
            "கட்டணம் முழுமையாக செலுத்தப்படவில்லை. பாதுகாப்பாக இங்கே முடிக்கலாம்: {link}"
        ),
        "alternate": (
            "வணக்கம் {first_name}, {merchant} நிறுவனத்திற்கான உங்கள் {amount} "
            "கட்டணம் செலுத்தப்படவில்லை. UPI, கார்டு அல்லது நெட்பேங்கிங் மூலம் இங்கே "
            "செலுத்தலாம்: {link}"
        ),
    },
}


def render_template(req: MessageRequest) -> str:
    """Render the fallback template for the request's language."""
    lang = req.language if req.language in _TEMPLATES else "en"
    variant = "alternate" if req.mention_alternate_methods else "standard"
    template = _TEMPLATES[lang][variant]
    return template.format(
        first_name=req.first_name or "there",
        amount=format_inr(req.amount_paise),
        link=req.link,
        merchant=req.merchant_name,
    )
