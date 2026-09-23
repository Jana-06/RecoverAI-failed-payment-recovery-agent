"""Message validator — the strict gate between the LLM and the customer.

If validation fails, the message is NEVER sent; the template fallback fires
instead. The validator is deliberately paranoid:

- exact amount string must appear (no amount tampering, no rounding)
- exact payment link must appear (no substituted/shortened URLs)
- <= 300 chars (SMS-friendly)
- correct language script (Latin for en, Devanagari for hi, Tamil for ta)
- no discount/refund/waiver promises (RecoverAI never offers money off)
- no urgency threats (deadline pressure / negative consequences)
- no added URLs (only the sanctioned link may appear)
- no phone numbers added back by the model (PII discipline)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.utils import format_inr

MAX_MESSAGE_CHARS = 300

# Scripts: Devanagari U+0900–U+097F, Tamil U+0B80–U+0BFF.
_SCRIPT_RANGES = {
    "en": None,  # Latin — checked implicitly (must not require other scripts)
    "hi": r"[\u0900-\u097F]",
    "ta": r"[\u0B80-\u0BFF]",
}

# Any promise of money off, however phrased. English carries the main load;
# Hindi/Tamil keywords (छूट discount, रिफंड refund, कैशबैक cashback, माफ़ waiver,
# தள்ளுபடி discount, ரீஃபண்ட் refund, கேஷ்பேக் cashback, சலுகை concession) are
# defense-in-depth for the vernacular messages.
_FORBIDDEN_DISCOUNT = re.compile(
    r"(discount|cashback|cash[- ]?back|refund|waiver|waive|scratch\s*card|"
    r"coupon|promo|special offer|\d{1,2}\s*%\s*(off|discount|cashback)|"
    r"छूट|रिफंड|कैशबैक|माफ़|"
    r"தள்ளுபடி|ரீஃபண்ட்|கேஷ்பேக்|சலுகை)",
    re.IGNORECASE,
)

# Urgency/threat patterns: deadlines, consequences, legal/account-action words.
_FORBIDDEN_URGENCY = re.compile(
    r"(today only|last chance|hurry|final notice|act now|"
    r"expire[sd]? (today|tomorrow|in \d)|deadline|"
    r"legal action|account (will be|shall be) (blocked|suspended|closed)|"
    r"बंद हो जाएगा|कानूनी कार्रवाई|तुरंत भुगतान|"
    r"இன்று மட்டும்|கடைசி வாய்ப்பு|உடனடியாக செலுத்த)",
    re.IGNORECASE,
)

_PHONE = re.compile(r"(?:\+?\d[\d\s-]{7,}\d)")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def validate_message(
    message: str,
    *,
    amount_paise: int,
    link: str,
    language: str,
    customer_name: str = "",
) -> ValidationResult:
    """Validate an LLM-drafted message against hard requirements.

    Returns ok=False with ALL violations listed (better debugging than
    fail-fast).
    """
    errors: list[str] = []
    text = (message or "").strip()

    if not text:
        return ValidationResult(ok=False, errors=["empty_message"])

    # 1. Length.
    if len(text) > MAX_MESSAGE_CHARS:
        errors.append(f"too_long({len(text)}>{MAX_MESSAGE_CHARS})")

    # 2. Exact amount: the canonical formatted string must appear verbatim.
    amount_str = format_inr(amount_paise)  # e.g. "Rs 500.00"
    if amount_str not in text:
        errors.append(f"amount_missing_or_altered(expected '{amount_str}')")

    # 3. Exact link.
    if link not in text:
        errors.append("link_missing_or_altered")

    # 4. No unsanctioned URLs.
    urls = re.findall(r"https?://\S+", text)
    for url in urls:
        if url.rstrip(".,)") != link.rstrip(".,)"):
            errors.append(f"foreign_url({url[:40]})")

    # 5. Language script check.
    pattern = _SCRIPT_RANGES.get(language)
    if pattern:
        if not re.search(pattern, text):
            errors.append(f"wrong_script(expected {language})")
    else:
        # English: reject messages dominated by non-Latin scripts.
        deva = len(re.findall(r"[\u0900-\u097F]", text))
        tamil = len(re.findall(r"[\u0B80-\u0BFF]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        if latin < deva + tamil:
            errors.append("wrong_script(expected en/Latin)")

    # 6. No discount/refund promises (English + hi/ta keywords).
    if _FORBIDDEN_DISCOUNT.search(text):
        errors.append("discount_or_refund_promise")

    # 7. No urgency threats.
    if _FORBIDDEN_URGENCY.search(text):
        errors.append("urgency_threat")

    # 8. No PII leakage: full phone numbers must not appear.
    if _PHONE.search(text.replace(link, "")):
        errors.append("phone_number_in_message")

    # 9. Personalization sanity: if a name is provided, it should appear.
    if customer_name and customer_name.split()[0].lower() not in text.lower():
        errors.append("missing_personalization")

    return ValidationResult(ok=not errors, errors=errors)
