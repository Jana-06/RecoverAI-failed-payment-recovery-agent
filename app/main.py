"""RecoverAI FastAPI application.

Phase 1 surface: POST /webhook/payment_failed (signature-verified) + health.
Phases 2-5 add /api/* endpoints and the dashboard (static/index.html).
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.ingest import upsert_failed_payment
from app.webhooks import parse_event, verify_webhook_signature
from app.api import router as api_router

app = FastAPI(title="RecoverAI", version="0.1.0")
app.include_router(api_router)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
_INDEX = os.path.join(_STATIC_DIR, "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "llm_configured": bool(settings.gemini_api_key),
        "razorpay_mode": "real_test_mode"
        if settings.razorpay_key_id
        else "simulated",
        "chaos": settings.chaos,
    }


@app.post("/webhook/payment_failed")
async def webhook_payment_failed(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Razorpay payment.failed webhook.

    Verifies X-Razorpay-Signature (HMAC-SHA256 of the RAW body with the
    webhook secret) BEFORE parsing/trusting the payload. Invalid signature
    -> 400 and the body is never processed.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_webhook_signature(raw, signature, settings.razorpay_webhook_secret):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_signature"})

    try:
        event = parse_event(raw)
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_payload"})

    if event.event != "payment.failed":
        # Acknowledge unhandled events so Razorpay stops retrying them.
        return JSONResponse(status_code=200, content={"ok": True, "handled": False})

    entity = event.payment_entity()
    if not entity.id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "missing_payment_id"})

    try:
        payment, created = upsert_failed_payment(db, entity)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "handled": True,
            "payment_id": payment.payment_id,
            "created": created,  # False => replay of a known webhook
        },
    )


@app.get("/", response_model=None)  # union Response types can't be a pydantic model
def index() -> FileResponse | JSONResponse:
    """Serve the dashboard (added in Phase 5)."""
    if os.path.exists(_INDEX):
        return FileResponse(_INDEX)
    return JSONResponse(
        status_code=200,
        content={"app": "RecoverAI", "status": "ok", "dashboard": "coming in phase 5"},
    )
