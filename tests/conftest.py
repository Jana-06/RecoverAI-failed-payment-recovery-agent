"""Pytest configuration.

Env must be set BEFORE any app import because the DB engine is created at
import time. Tests always run with: no Gemini key (template fallback), no
Razorpay keys (simulated client), chaos off, and an isolated SQLite file.
"""

from __future__ import annotations

import os

os.environ["RECOVERAI_DB"] = "data/test_recoverai.db"
os.environ["GEMINI_API_KEY"] = ""  # force template fallback in tests
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")
os.environ.pop("CHAOS", None)  # chaos off unless a test opts in

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> None:
    from app.db import Base, engine
    from app import models  # noqa: F401  (registers ORM tables)

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture()
def db_session():
    """Session with per-test cleanup of all rows (fresh state every test)."""
    from app.db import SessionLocal
    from app.models import AuditLog, Customer, Decision, IdempotencyKey, Payment

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        # Children before parents (FKs are enforced via PRAGMA).
        for model in (Decision, AuditLog, IdempotencyKey, Payment, Customer):
            session.query(model).delete()
        session.commit()
        session.close()
