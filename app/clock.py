"""Simulation clock.

Everything in the app asks this module for "now" instead of calling
time.time()/datetime.now() directly, so the demo can fast-forward time
(e.g. "Fast-forward 24h" button) and time-based rules (R2's 24h wait,
quiet hours) actually fire.
"""

from __future__ import annotations

import threading
import time as _time

_lock = threading.Lock()
_offset_seconds: float = 0.0


def now_ts() -> int:
    """Current epoch seconds, including any fast-forward offset."""
    with _lock:
        return int(_time.time() + _offset_seconds)


def offset_seconds() -> float:
    with _lock:
        return _offset_seconds


def advance(seconds: float) -> int:
    """Fast-forward the sim clock; returns the new now_ts()."""
    global _offset_seconds
    if seconds < 0:
        raise ValueError("cannot rewind the simulation clock")
    with _lock:
        _offset_seconds += seconds
        return int(_time.time() + _offset_seconds)


def reset() -> None:
    """Clear any fast-forward offset (used by tests)."""
    global _offset_seconds
    with _lock:
        _offset_seconds = 0.0


def set_offset(seconds: float) -> int:
    """Pin the sim clock to an absolute offset (test/demo determinism helper).

    Unlike advance(), this may move the clock backward — legitimate for
    pinning a reproducible demo time, never used by business logic.
    """
    global _offset_seconds
    with _lock:
        _offset_seconds = seconds
        return int(_time.time() + _offset_seconds)
