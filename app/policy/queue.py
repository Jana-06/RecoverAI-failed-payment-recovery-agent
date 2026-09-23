"""Queue builder: DB -> PolicyInput -> engine -> prioritized queue.

Priority = amount × estimated recovery probability, sorted descending by
expected recovered revenue (highest rupee impact first).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.clock import now_ts
from app.config import settings
from app.models import AuditLog, Decision, Payment
from app.policy import engine
from app.policy.classify import Classification, classify
from app.policy.probability import get_probability
from app.utils import ist_day_bounds, ts_to_ist


@dataclass
class QueueItem:
    payment_id: str
    order_id: str
    amount_paise: int
    method: str
    category: str
    reason: str
    action: str
    rule_id: str
    engine_reason: str
    expected_recovery_paise: int
    estimated_probability: float
    recovery_attempts: int
    eligible_at_ts: int | None
    customer_name: str
    customer_language: str
    opted_out: bool
    needs_human_review: bool
    human_approved: bool
    created_at: int

    def to_api(self) -> dict[str, Any]:
        d = asdict(self)
        d["eligible_at_iso"] = (
            ts_to_ist(self.eligible_at_ts).strftime("%Y-%m-%d %H:%M IST")
            if self.eligible_at_ts
            else None
        )
        return d


def _classification_for(payment: Payment) -> Classification:
    return classify(
        error_reason=payment.error_reason,
        error_description=payment.error_description,
        error_source=payment.error_source,
        error_step=payment.error_step,
    )


def _human_approved(payment: Payment) -> bool:
    return bool(payment.needs_human_review and payment.review_cleared_at is not None)


def _state_from_audit(
    db: Session, payment_ids: list[str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Derive (last_action_ts, last_wait_ts) per payment from the audit trail.

    last_action: epoch of the latest executed retry/nudge (G6 cooldown).
    last_wait:   epoch of the latest R2 wait-start (24h wait bookkeeping).
    """
    if not payment_ids:
        return {}, {}
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.payment_id.in_(payment_ids),
            AuditLog.action.in_(["retry", "nudge", "wait"]),
            AuditLog.outcome.in_(["retry_scheduled", "message_sent", "wait_started"]),
        )
        .all()
    )
    last_action: dict[str, int] = {}
    last_wait: dict[str, int] = {}
    for r in rows:
        if r.action in ("retry", "nudge"):
            last_action[r.payment_id] = max(last_action.get(r.payment_id, 0), r.ts)
        elif r.action == "wait":
            last_wait[r.payment_id] = max(last_wait.get(r.payment_id, 0), r.ts)
    return last_action, last_wait


def build_queue(db: Session, persist: bool = True) -> list[QueueItem]:
    """Decide every open payment; optionally persist Decision rows; sort by ₹ impact."""
    now = now_ts()
    day_start, day_end = ist_day_bounds(now)
    messages_today = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "nudge",
            AuditLog.outcome == "message_sent",
            AuditLog.ts >= day_start,
            AuditLog.ts < day_end,
        )
        .count()
    )

    items: list[QueueItem] = []
    new_decisions: list[Decision] = []
    open_payments = (
        db.query(Payment)
        .filter(Payment.status == "open")
        .order_by(Payment.amount_paise.desc())
        .all()
    )
    last_action_map, last_wait_map = _state_from_audit(
        db, [p.payment_id for p in open_payments]
    )
    for p in open_payments:
        classification = _classification_for(p)
        inp = engine.PolicyInput(
            payment_id=p.payment_id,
            amount_paise=p.amount_paise,
            method=p.method,
            classification=classification,
            customer_opted_out=bool(p.customer.opted_out),
            recovery_attempts=p.recovery_attempts,
            messages_sent_today=messages_today,
            daily_budget=settings.daily_message_budget,
            high_value_paise=settings.high_value_paise,
            now_ts=now,
            human_approved=_human_approved(p),
            last_action_ts=last_action_map.get(p.payment_id),
            last_wait_ts=last_wait_map.get(p.payment_id),
        )
        decision = engine.decide(inp)

        if persist:
            new_decisions.append(Decision(
                payment_fk=p.id,
                created_at=now,
                category=classification.category,
                action=decision.action,
                rule_id=decision.rule_id,
                reason=decision.reason,
                inputs_json=decision.inputs,
                expected_recovery_paise=decision.expected_recovery_paise,
                estimated_probability=decision.estimated_probability,
            ))

        items.append(QueueItem(
            payment_id=p.payment_id,
            order_id=p.order_id,
            amount_paise=p.amount_paise,
            method=p.method,
            category=classification.category,
            reason=classification.reason,
            action=decision.action,
            rule_id=decision.rule_id,
            engine_reason=decision.reason,
            expected_recovery_paise=decision.expected_recovery_paise,
            estimated_probability=decision.estimated_probability,
            recovery_attempts=p.recovery_attempts,
            eligible_at_ts=decision.eligible_at_ts,
            customer_name=p.customer.name if p.customer else "",
            customer_language=p.customer.language if p.customer else "en",
            opted_out=bool(p.customer.opted_out),
            needs_human_review=p.needs_human_review or decision.action == engine.ACTION_REVIEW,
            human_approved=_human_approved(p),
            created_at=p.created_at,
        ))

    if persist and new_decisions:
        db.add_all(new_decisions)
        db.commit()

    # Priority queue: highest expected recovered ₹ first.
    items.sort(key=lambda it: it.expected_recovery_paise, reverse=True)
    return items
