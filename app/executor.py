"""Executor: turns approved policy decisions into actions — with receipts.

Responsibilities:
- Run the policy queue, executing retry / nudge / wait / hold / review actions.
- IDEMPOTENCY: every action writes its key into idempotency_keys BEFORE
  acting; a repeated run (crash recovery, double-click, replay) can never
  nudge the same payment twice at the same attempt ordinal.
- APPEND-ONLY AUDIT: every action, skip, hold, and error becomes an
  audit_log row (insert-only; never updated or deleted).
- SEEDED OUTCOMES: recovery results come from a deterministic per-payment
  RNG documented below. ALL outcomes are SIMULATED.

Simulation model (documented assumptions):
- After a customer-visible action (retry/nudge), recovery may happen within
  a 24h window. The probability used is the engine's own estimated
  probability at decision time (24h horizon, includes attempt falloff).
- The roll is `random.Random(f"outcome:{payment_id}:{ordinal}").random()`,
  so the same payment + attempt always produces the same result regardless
  of when/how often the agent runs — the demo is reproducible.
- No discounts, no partial recoveries: recovered amount == payment amount.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import now_ts
from app.config import settings
from app.razorpay.factory import get_razorpay_client
from app.razorpay.base import RazorpayClient
from app.razorpay.types import RazorpayCustomer
from app.llm.gemini import MessageRequest
from app.llm.writer import compose_message
from app.models import AuditLog, Decision, IdempotencyKey, Payment
from app.policy import engine
from app.policy.queue import QueueItem, build_queue
from app.utils import format_inr, ist_day_bounds

logger = logging.getLogger("recoverai.executor")

# Grace period after an action before its outcome may resolve.
OUTCOME_GRACE_SECONDS = 2 * 3600  # 2h
MERCHANT_NAME = "Acme Retail"  # demo merchant


@dataclass
class RunSummary:
    ran_at: int
    decided: int = 0
    retries: int = 0
    nudges: int = 0
    waits: int = 0
    holds: int = 0
    ignores: int = 0
    reviews: int = 0
    errors: int = 0
    recovered_now: int = 0
    recovered_amount_paise: int = 0
    llm_used: int = 0
    template_used: int = 0
    fallbacks: int = 0
    notes: list[str] = field(default_factory=list)


def _audit(
    db: Session,
    *,
    payment_id: str,
    action: str,
    outcome: str,
    rule_id: str = "",
    reason: str = "",
    message_source: str = "system",
    message_text: str = "",
    detail: dict | None = None,
) -> None:
    db.add(AuditLog(
        ts=now_ts(),
        payment_id=payment_id,
        rule_id=rule_id,
        action=action,
        reason=reason[:255],
        message_source=message_source,
        message_text=message_text,
        outcome=outcome,
        detail=detail or {},
    ))


def _claim_idempotency_key(db: Session, key: str) -> bool:
    """Insert-first claim; False if the key already exists (action already done).

    The insert commits before the action runs, so a crash mid-action leaves
    the key claimed (the action is retried manually, never duplicated).
    """
    db.add(IdempotencyKey(key=key, created_at=now_ts()))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _release_idempotency_key(db: Session, key: str) -> None:
    """Roll back a claim after a HANDLED failure (e.g. link creation error).

    Semantics: a crash keeps the key (never double-send); a known failure
    releases it so a later run can retry the same attempt ordinal. Without
    this, one chaos/network error would permanently block the payment's
    nudge (key claimed, ordinal never advanced).
    """
    row = db.query(IdempotencyKey).filter(IdempotencyKey.key == key).one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()


def _latest_probability(db: Session, payment: Payment) -> float:
    row = (
        db.query(Decision)
        .filter(Decision.payment_fk == payment.id)
        .order_by(Decision.created_at.desc(), Decision.id.desc())
        .first()
    )
    return float(row.estimated_probability) if row else 0.0


class Executor:
    def __init__(self, db: Session, client: RazorpayClient | None = None) -> None:
        self.db = db
        self.client = client or get_razorpay_client()

    # ------------------------------------------------------------- main run
    def run_agent(self) -> RunSummary:
        summary = RunSummary(ran_at=now_ts())
        items = build_queue(self.db, persist=True)
        summary.decided = len(items)

        for item in items:
            try:
                self._execute(item, summary)
            except Exception as exc:  # never let one row kill the run
                summary.errors += 1
                logger.exception("executor error for %s", item.payment_id)
                _audit(
                    self.db, payment_id=item.payment_id, action=item.action,
                    outcome="error", rule_id=item.rule_id,
                    reason=f"executor exception: {exc}",
                )
                self.db.commit()

        summary.recovered_now, summary.recovered_amount_paise = self.resolve_due_outcomes()
        return summary

    # ---------------------------------------------------------- per action
    def _execute(self, item: QueueItem, summary: RunSummary) -> None:
        if item.action == engine.ACTION_RETRY:
            self._do_retry(item, summary)
        elif item.action == engine.ACTION_NUDGE:
            self._do_nudge(item, summary)
        elif item.action == engine.ACTION_WAIT:
            self._do_wait(item, summary)
        elif item.action == engine.ACTION_HOLD:
            self._do_hold(item, summary)
        elif item.action == engine.ACTION_IGNORE:
            self._do_ignore(item, summary)
        elif item.action == engine.ACTION_REVIEW:
            self._do_review(item, summary)

    def _do_retry(self, item: QueueItem, summary: RunSummary) -> None:
        ordinal = item.recovery_attempts + 1
        key = f"{item.payment_id}:retry:{ordinal}"
        if not _claim_idempotency_key(self.db, key):
            return  # already retried at this ordinal — replay-safe no-op

        try:
            payment = self._payment(item.payment_id)
            payment.recovery_attempts += 1
            # Outcome may resolve after backoff + grace.
            payment.pending_outcome_at = (item.eligible_at_ts or now_ts()) + OUTCOME_GRACE_SECONDS
            payment.status = "open"
            _audit(
                self.db, payment_id=item.payment_id, action="retry",
                outcome="retry_scheduled", rule_id=item.rule_id, reason=item.engine_reason,
                detail={"ordinal": ordinal, "eligible_at_ts": item.eligible_at_ts,
                        "expected_recovery_paise": item.expected_recovery_paise},
            )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            _release_idempotency_key(self.db, key)
            _audit(
                self.db, payment_id=item.payment_id, action="retry",
                outcome="error", rule_id=item.rule_id,
                reason=f"retry failed after claim: {exc}",
            )
            self.db.commit()
            summary.errors += 1
            return
        summary.retries += 1

    def _do_nudge(self, item: QueueItem, summary: RunSummary) -> None:
        ordinal = item.recovery_attempts + 1
        key = f"{item.payment_id}:nudge:{ordinal}"
        if not _claim_idempotency_key(self.db, key):
            return

        payment = self._payment(item.payment_id)

        try:
            # 1. Fresh payment link (same amount — never discounted).
            link_result = self.client.create_payment_link(
                amount=payment.amount_paise,
                customer=RazorpayCustomer(name=item.customer_name),
                description=f"RecoverAI retry for {item.payment_id}",
                reference_id=f"recoverai:{item.payment_id}:{ordinal}",
            )
            if not link_result.ok or link_result.link is None:
                _audit(
                    self.db, payment_id=item.payment_id, action="nudge",
                    outcome="error", rule_id=item.rule_id,
                    reason=f"payment link creation failed: {link_result.error}",
                    detail={"ordinal": ordinal},
                )
                self.db.commit()
                # Release the claim: the attempt never happened, a later run may
                # retry this ordinal (see _release_idempotency_key).
                _release_idempotency_key(self.db, key)
                summary.errors += 1
                return
            link = link_result.link

            # 2. Compose message (LLM -> validate -> template floor).
            outcome_msg = compose_message(MessageRequest(
                first_name=(item.customer_name or "there").split()[0],
                amount_paise=payment.amount_paise,
                language=item.customer_language or "en",
                merchant_name=MERCHANT_NAME,
                link=link.short_url,
                tone="friendly",
                mention_alternate_methods=(item.rule_id == "R2"),
            ))
            if outcome_msg.source == "llm":
                summary.llm_used += 1
            else:
                summary.template_used += 1
                if outcome_msg.fallback_used:
                    summary.fallbacks += 1

            # 3. "Send" + audit with full metrics.
            payment.recovery_attempts += 1
            payment.last_link_id = link.id
            payment.last_link_url = link.short_url
            payment.pending_outcome_at = now_ts() + OUTCOME_GRACE_SECONDS
            _audit(
                self.db, payment_id=item.payment_id, action="nudge",
                outcome="message_sent", rule_id=item.rule_id, reason=item.engine_reason,
                message_source=outcome_msg.source, message_text=outcome_msg.text,
                detail={
                    "ordinal": ordinal,
                    "link_id": link.id,
                    "amount_str": format_inr(payment.amount_paise),
                    **outcome_msg.to_detail(),
                },
            )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            _release_idempotency_key(self.db, key)
            _audit(
                self.db, payment_id=item.payment_id, action="nudge",
                outcome="error", rule_id=item.rule_id,
                reason=f"nudge failed after claim: {exc}",
                detail={"ordinal": ordinal},
            )
            self.db.commit()
            summary.errors += 1
            return
        summary.nudges += 1

    def _do_wait(self, item: QueueItem, summary: RunSummary) -> None:
        key = f"{item.payment_id}:wait"
        if not _claim_idempotency_key(self.db, key):
            return  # wait already started
        _audit(
            self.db, payment_id=item.payment_id, action="wait",
            outcome="wait_started", rule_id=item.rule_id, reason=item.engine_reason,
            detail={"eligible_at_ts": item.eligible_at_ts},
        )
        self.db.commit()
        summary.waits += 1

    def _do_hold(self, item: QueueItem, summary: RunSummary) -> None:
        # One hold row per rule per IST day (otherwise every agent run spams).
        day = ist_day_bounds(now_ts())[0]
        key = f"{item.payment_id}:hold:{item.rule_id}:{day}"
        if not _claim_idempotency_key(self.db, key):
            return
        _audit(
            self.db, payment_id=item.payment_id, action="hold",
            outcome="held", rule_id=item.rule_id, reason=item.engine_reason,
            detail={"eligible_at_ts": item.eligible_at_ts},
        )
        self.db.commit()
        summary.holds += 1

    def _do_ignore(self, item: QueueItem, summary: RunSummary) -> None:
        key = f"{item.payment_id}:ignore:{item.rule_id}"
        if not _claim_idempotency_key(self.db, key):
            return
        _audit(
            self.db, payment_id=item.payment_id, action="ignore",
            outcome="skipped_guardrail", rule_id=item.rule_id,
            reason=item.engine_reason,
        )
        self.db.commit()
        summary.ignores += 1

    def _do_review(self, item: QueueItem, summary: RunSummary) -> None:
        key = f"{item.payment_id}:review"
        if not _claim_idempotency_key(self.db, key):
            return
        payment = self._payment(item.payment_id)
        payment.needs_human_review = True
        _audit(
            self.db, payment_id=item.payment_id, action="review",
            outcome="flagged", rule_id=item.rule_id, reason=item.engine_reason,
            detail={"expected_recovery_paise": item.expected_recovery_paise},
        )
        self.db.commit()
        summary.reviews += 1

    def _payment(self, payment_id: str) -> Payment:
        payment = self.db.query(Payment).filter(Payment.payment_id == payment_id).one()
        return payment

    # -------------------------------------------------- seeded outcomes (SIM)
    def resolve_due_outcomes(self) -> tuple[int, int]:
        """Resolve pending simulated outcomes whose grace window has elapsed.

        SIMULATED, seeded per (payment_id, ordinal) so results are
        reproducible. Returns (count_recovered, amount_recovered_paise).
        """
        now = now_ts()
        due = (
            self.db.query(Payment)
            .filter(
                Payment.status == "open",
                Payment.recovery_attempts > 0,
                Payment.pending_outcome_at.isnot(None),
                Payment.pending_outcome_at <= now,
            )
            .all()
        )
        recovered = 0
        amount = 0
        for payment in due:
            ordinal = payment.recovery_attempts
            p = _latest_probability(self.db, payment)
            roll = random.Random(f"outcome:{payment.payment_id}:{ordinal}").random()
            payment.pending_outcome_at = None
            if roll < p:
                payment.status = "recovered"
                payment.recovered_at = now
                payment.recovered_amount_paise = payment.amount_paise
                _audit(
                    self.db, payment_id=payment.payment_id, action="system",
                    outcome="recovered",
                    reason=f"simulated recovery (p={p:.2f}, roll={roll:.3f})",
                    detail={"simulated": True, "probability": p, "roll": round(roll, 4),
                            "ordinal": ordinal},
                )
                recovered += 1
                amount += payment.amount_paise
            else:
                _audit(
                    self.db, payment_id=payment.payment_id, action="system",
                    outcome="no_response",
                    reason=f"simulated non-recovery (p={p:.2f}, roll={roll:.3f})",
                    detail={"simulated": True, "probability": p, "roll": round(roll, 4),
                            "ordinal": ordinal},
                )
        if due:
            self.db.commit()
        return recovered, amount
