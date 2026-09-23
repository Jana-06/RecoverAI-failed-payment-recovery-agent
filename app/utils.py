"""Shared helpers: PII masking, INR formatting, IST time handling.

Business rules (quiet hours, 24h waits) are computed in IST (UTC+05:30) because
that's the customer's clock, even though timestamps are stored as UTC epoch seconds.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def mask_phone(phone: str | None) -> str:
    """Keep only the last 4 digits: '9876543210' -> '******3210'.

    Normalizes Indian formats first: '+919876543210' and '09876543210'
    both reduce to the 10-digit national number before masking.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def format_inr(paise: int) -> str:
    """123456700 paise -> 'Rs 12,34,567.00' (Indian digit grouping)."""
    rupees = paise / 100
    # Strip western grouping Python adds, then re-group Indian-style:
    # last 3 digits together, then pairs: 1234567 -> 12,34,567.
    whole, _, frac = f"{rupees:.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"Rs {whole}.{frac}"


def ts_to_ist(ts: int) -> datetime:
    """Epoch seconds -> aware datetime in IST."""
    return datetime.fromtimestamp(ts, tz=IST)


def ist_hour(ts: int) -> int:
    """Hour of day (0-23) in IST for the given epoch seconds."""
    return ts_to_ist(ts).hour


def ist_date_str(ts: int) -> str:
    """YYYY-MM-DD in IST — the key for daily budgets."""
    return ts_to_ist(ts).strftime("%Y-%m-%d")


def ts_to_utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_day_bounds(ts: int) -> tuple[int, int]:
    """(start_ts, end_ts_exclusive) of the IST day containing `ts`.

    Used for daily budgets: count events whose sim-timestamp falls in the
    same IST calendar day, independent of storage format.
    """
    ist_dt = ts_to_ist(ts)
    start = ist_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())
