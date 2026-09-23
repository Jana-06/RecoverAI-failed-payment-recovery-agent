"""Dashboard API endpoints (Phase 4/5 surface).

All numbers returned here are SIMULATED and labeled as such in the UI.
Endpoints are unauthenticated by design (buildathon demo, localhost).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.clock import advance, now_ts, offset_seconds, reset
from app.config import settings
from app.db import get_db
from app.executor import MERCHANT_NAME, Executor
from app.llm.gemini import MessageRequest
from app.llm.writer import compose_message
from app.models import AuditLog, Payment
from app.policy import engine
from app.policy.queue import build_queue
from app.utils import format_inr, ts_to_utc_iso

router = APIRouter(prefix="/api")


@router.get("/kpis")
def kpis(db: Session = Depends(get_db)) -> dict:
    total_failed = 0
    recovered_paise = 0
    recovered_count = 0
    at_risk = 0
    review_count = 0
    failed_count = 0
    for p in db.query(Payment).all():
        total_failed += p.amount_paise
        failed_count += 1
        if p.status == "recovered":
            recovered_paise += p.recovered_amount_paise or 0
            recovered_count += 1
        elif p.status == "open":
            at_risk += p.amount_paise
            # Count only items still AWAITING a human decision (approved ones
            # leave the counter; they're nudge-eligible on the next run).
            if p.needs_human_review and p.review_cleared_at is None:
                review_count += 1
    rate = (recovered_count / failed_count * 100) if failed_count else 0.0
    return {
        "simulated": True,
        "total_failed_paise": total_failed,
        "total_failed_display": format_inr(total_failed),
        "recovered_paise": recovered_paise,
        "recovered_display": format_inr(recovered_paise),
        "recovery_rate_pct": round(rate, 2),
        "at_risk_paise": at_risk,
        "at_risk_display": format_inr(at_risk),
        "human_review_count": review_count,
        "failed_count": failed_count,
        "recovered_count": recovered_count,
        "now_ts": now_ts(),
        "now_iso": ts_to_utc_iso(now_ts()),
        "clock_offset_seconds": offset_seconds(),
    }


@router.get("/queue")
def queue(db: Session = Depends(get_db)) -> dict:
    items = build_queue(db, persist=False)
    return {
        "simulated": True,
        "items": [it.to_api() for it in items],
    }


@router.post("/agent/run")
def agent_run(db: Session = Depends(get_db)) -> JSONResponse:
    """Run the agent once over all open payments. Idempotent per attempt ordinal."""
    summary = Executor(db).run_agent()
    return JSONResponse(content={
        "simulated": True,
        "ran_at": summary.ran_at,
        "decided": summary.decided,
        "retries": summary.retries,
        "nudges": summary.nudges,
        "waits": summary.waits,
        "holds": summary.holds,
        "ignores": summary.ignores,
        "reviews": summary.reviews,
        "errors": summary.errors,
        "recovered_now": summary.recovered_now,
        "recovered_amount_paise": summary.recovered_amount_paise,
        "llm_used": summary.llm_used,
        "template_used": summary.template_used,
        "fallbacks": summary.fallbacks,
    })


class AdvanceRequest(BaseModel):
    hours: float = 24.0


@router.post("/clock/advance")
def clock_advance(req: AdvanceRequest, db: Session = Depends(get_db)) -> dict:
    """Fast-forward the simulation clock, then resolve any due outcomes.

    For a 24h jump, the clock extends to the next 08:00 IST if the target
    lands inside quiet hours — otherwise a night-time demo would advance a
    full day and STILL hold every contact (G4), which confused nobody once
    but would confuse everyone at a midnight demo.
    """
    if req.hours <= 0 or req.hours > 24 * 30:
        raise HTTPException(status_code=400, detail="hours must be in (0, 720]")
    total_hours = req.hours
    new_now = advance(req.hours * 3600)
    if req.hours == 24.0 and engine.is_quiet_hours(new_now):
        # Hours until the next 08:00 IST wall clock.
        ist_hour_now = (new_now + 5 * 3600 + 30 * 60) % 86400 / 3600.0
        hours_to_8am = (32 - ist_hour_now) % 24 or 24
        new_now = advance(hours_to_8am * 3600)
        total_hours = req.hours + hours_to_8am
    recovered, amount = Executor(db).resolve_due_outcomes()
    return {
        "simulated": True,
        "advanced_hours": round(total_hours, 2),
        "now_ts": new_now,
        "now_iso": ts_to_utc_iso(new_now),
        "outcomes_resolved": recovered,
        "recovered_paise": amount,
    }


@router.post("/clock/reset")
def clock_reset() -> dict:
    reset()
    return {"simulated": True, "now_ts": now_ts(), "clock_offset_seconds": 0}


@router.get("/audit")
def audit(limit: int = 60, db: Session = Depends(get_db)) -> dict:
    rows = (
        db.query(AuditLog)
        .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return {
        "simulated": True,
        "rows": [
            {
                "ts": r.ts,
                "ts_iso": ts_to_utc_iso(r.ts),
                "payment_id": r.payment_id,
                "rule_id": r.rule_id,
                "action": r.action,
                "outcome": r.outcome,
                "reason": r.reason,
                "message_source": r.message_source,
                "message_text": r.message_text,
                "detail": r.detail or {},
            }
            for r in rows
        ],
    }


@router.get("/chart_data")
def chart_data(db: Session = Depends(get_db)) -> dict:
    """Failures by reason + baseline-vs-agent recovery on the same data."""
    by_reason: dict[str, int] = {}
    by_amount: dict[str, int] = {}
    baseline_recovered_paise = 0  # seeded already-recovered: no agent involved
    agent_recovered_paise = 0
    agent_recovered_count = 0
    for p in db.query(Payment).all():
        reason = p.error_reason or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_amount[reason] = by_amount.get(reason, 0) + p.amount_paise
        if p.status == "recovered":
            if p.recovery_attempts > 0:
                agent_recovered_paise += p.recovered_amount_paise or 0
                agent_recovered_count += 1
            else:
                baseline_recovered_paise += p.recovered_amount_paise or 0
    total_paise = sum(by_amount.values()) or 1
    return {
        "simulated": True,
        "failures_by_reason": by_reason,
        "amount_by_reason": by_amount,
        "recovery_comparison": {
            # Baseline = organic recoveries on the same seeded data (no agent).
            "baseline_recovered_paise": baseline_recovered_paise,
            # RecoverAI = everything the agent recovered, in sim.
            "agent_recovered_paise": agent_recovered_paise,
            "agent_recovered_count": agent_recovered_count,
            "total_failed_paise": total_paise,
            "note": "SIMULATED outcomes from the seeded simulator; not real results",
        },
    }


@router.get("/payments/{payment_id}/message")
def message_preview(payment_id: str, db: Session = Depends(get_db)) -> dict:
    """Last SENT message for the payment from the audit trail; else a live
    preview (composed now, not sent, not audited)."""
    row = (
        db.query(AuditLog)
        .filter(AuditLog.payment_id == payment_id, AuditLog.outcome == "message_sent")
        .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
        .first()
    )
    if row:
        d = row.detail or {}
        return {
            "payment_id": payment_id,
            "preview": False,
            "text": row.message_text,
            "source": row.message_source,
            "fallback_used": d.get("fallback_used", False),
            "fallback_reason": d.get("fallback_reason", ""),
            "llm_latency_ms": d.get("llm_latency_ms", 0),
            "llm_output_tokens": d.get("llm_output_tokens", 0),
        }

    payment = db.query(Payment).filter(Payment.payment_id == payment_id).first()
    if payment is None:
        raise HTTPException(status_code=404, detail="payment not found")
    req = MessageRequest(
        first_name=(payment.customer.name if payment.customer else "there").split()[0],
        amount_paise=payment.amount_paise,
        language=(payment.customer.language if payment.customer else "en") or "en",
        merchant_name=MERCHANT_NAME,
        link=payment.last_link_url or f"https://rzp.io/i/preview_{payment_id[-6:]}",
        tone="friendly",
    )
    outcome = compose_message(req)
    return {
        "payment_id": payment_id,
        "preview": True,
        "text": outcome.text,
        "source": outcome.source,
        "fallback_used": outcome.fallback_used,
        "fallback_reason": outcome.fallback_reason,
        "llm_latency_ms": outcome.latency_ms,
        "llm_output_tokens": outcome.output_tokens,
    }


class ReviewDecision(BaseModel):
    approve: bool


@router.post("/payments/{payment_id}/review")
def review(payment_id: str, req: ReviewDecision, db: Session = Depends(get_db)) -> dict:
    """Human decision on a review item: approve (unlocks policy rules) or skip."""
    payment = db.query(Payment).filter(Payment.payment_id == payment_id).first()
    if payment is None:
        raise HTTPException(status_code=404, detail="payment not found")
    if req.approve:
        payment.review_cleared_at = now_ts()
        note, outcome = "approved by human", "approved"
    else:
        payment.status = "abandoned"
        payment.review_cleared_at = now_ts()
        note, outcome = "skipped by human", "skipped"
    db.add(AuditLog(
        ts=now_ts(), payment_id=payment_id, rule_id="",
        action="human_review", outcome=note, reason="dashboard decision",
        message_source="system", detail={"decision": outcome},
    ))
    db.commit()
    return {"payment_id": payment_id, "decision": outcome}


@router.get("/meta")
def meta() -> dict:
    return {
        "simulated": True,
        "llm_configured": bool(settings.gemini_api_key),
        "razorpay_mode": "real_test_mode" if settings.razorpay_key_id else "simulated",
        "chaos": settings.chaos,
        "high_value_paise": settings.high_value_paise,
        "daily_message_budget": settings.daily_message_budget,
        "max_recovery_attempts": engine.MAX_RECOVERY_ATTEMPTS,
        "quiet_hours": f"{engine.QUIET_HOURS_START:02d}:00-{engine.QUIET_HOURS_END:02d}:00 IST",
    }
