"""ORM models.

Design notes:
- Money is stored in **paise** (int) — never floats.
- Phones are stored **masked** (last 4 digits only); the app never persists a
  full phone number.
- audit_log is append-only: the API layer only ever INSERTs into it.
- decisions stores the policy engine's output per payment per run, including
  the full `inputs` JSON for explainability.
- idempotency_keys makes executor actions replay-safe (a nudge can't fire twice).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _ts_now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # customer_id is our stable identifier (synthetic "cust_..." for seed data,
    # or derived from webhook payload).
    customer_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    masked_phone: Mapped[str] = mapped_column(String(16), default="")
    # en | hi | ta — the language the customer sees messages in.
    language: Mapped[str] = mapped_column(String(8), default="en")
    # Hard guardrail: opted-out customers are NEVER contacted.
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)

    payments: Mapped[list["Payment"]] = relationship(back_populates="customer")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Razorpay-style identifiers, e.g. "pay_Qm9xYz..." / "order_Qm9xYz...".
    payment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)

    customer_fk: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    customer: Mapped[Customer | None] = relationship(back_populates="payments")

    amount_paise: Mapped[int] = mapped_column(Integer)  # amount in paise, always int
    currency: Mapped[str] = mapped_column(String(4), default="INR")
    # upi | card | netbanking | wallet
    method: Mapped[str] = mapped_column(String(16), index=True)

    # Razorpay-style error fields from payment.failed webhook payloads.
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_description: Mapped[str] = mapped_column(String(255), default="")
    error_source: Mapped[str] = mapped_column(String(32), default="")  # customer|gateway|bank|network
    error_step: Mapped[str] = mapped_column(String(32), default="")  # payment_initiation|authorization|...
    error_reason: Mapped[str] = mapped_column(String(64), default="", index=True)

    created_at: Mapped[int] = mapped_column(Integer, index=True)  # epoch seconds (UTC)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)

    # --- recovery state machine ---
    # status: open | recovered | abandoned | review
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # recovery_attempts counts contact/retry attempts made BY the agent
    # (bounded by MAX_RECOVERY_ATTEMPTS = 3).
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # payment_link_id of the most recent recovery link created for this payment.
    last_link_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_link_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    recovered_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recovered_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set when R4 (risk) or >high-value sends the payment to human review.
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_cleared_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sim-timestamp when the pending simulated outcome may resolve (None = none pending).
    pending_outcome_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[int] = mapped_column(Integer, default=_ts_now, onupdate=_ts_now)

    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan"
    )


class Decision(Base):
    """One row per (payment, agent run): what the engine decided and why."""

    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_payment_created", "payment_fk", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_fk: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    payment: Mapped[Payment] = relationship(back_populates="decisions")

    created_at: Mapped[int] = mapped_column(Integer, index=True)

    # classification
    category: Mapped[str] = mapped_column(String(32), index=True)

    # what the engine chose
    action: Mapped[str] = mapped_column(String(24))  # retry|wait|nudge|ignore|review
    rule_id: Mapped[str] = mapped_column(String(8))  # R1..R4
    reason: Mapped[str] = mapped_column(String(255))
    # Full explainability: all inputs the engine looked at (JSON).
    inputs_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # priority queue score: amount x estimated recovery probability
    expected_recovery_paise: Mapped[int] = mapped_column(Integer, default=0)
    estimated_probability: Mapped[float] = mapped_column(Float, default=0.0)


class AuditLog(Base):
    """Append-only audit trail. The API must only INSERT rows here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(Integer, index=True)  # epoch seconds (sim clock)
    payment_id: Mapped[str] = mapped_column(String(64), index=True)  # external id
    rule_id: Mapped[str] = mapped_column(String(8), default="")
    action: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(255), default="")
    # llm | template | system (system = retries/approvals/other non-message actions)
    message_source: Mapped[str] = mapped_column(String(12), default="system")
    message_text: Mapped[str] = mapped_column(Text, default="")
    # created | link_created | message_sent | retry_scheduled | recovered |
    # failed | skipped_blocked | error
    outcome: Mapped[str] = mapped_column(String(24), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class IdempotencyKey(Base):
    """Replay protection: one row per attempted action key.

    The executor computes e.g. key = f"{payment_id}:{action}:{date}" and only
    proceeds if the key doesn't exist yet (insert-first, commit, then act).
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("key", name="uq_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[int] = mapped_column(Integer)
