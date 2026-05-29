"""Retell webhook handler (Iter 1 stub).

Handles ``function_call`` for ``book_appointment`` against mock PMS data, plus
acknowledges ``call_started`` / ``call_ended``. Full call persistence and
idempotent dedup land in Phase 2.

Auth: ``X-Retell-Signature`` HMAC over the raw body, compared to
RETELL_WEBHOOK_SECRET. In weekend mode the secret may be empty; when empty we
skip verification (local testing) but log a warning.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.services.booking import find_available_slots

logger = logging.getLogger("dentiva.webhooks.retell")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = get_settings().retell_webhook_secret
    if not secret:
        logger.warning("RETELL_WEBHOOK_SECRET empty — skipping signature check (dev only).")
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/retell")
async def retell_webhook(request: Request) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("x-retell-signature")
    if not _verify_signature(raw_body, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad webhook signature."
        )

    payload = await request.json()
    event = payload.get("event")

    if event == "call_started":
        logger.info("Retell call_started: %s", payload.get("call_id"))
        return {"ok": True}

    if event == "call_ended":
        logger.info("Retell call_ended: %s", payload.get("call_id"))
        return {"ok": True}

    if event == "function_call":
        fn = payload.get("function_name")
        args = payload.get("args", {}) or {}
        if fn == "book_appointment":
            slots = await find_available_slots(
                procedure=args.get("procedure", "cleaning"),
                preferred_date=args.get("preferred_date", ""),
                preferred_time_window=args.get("preferred_time_window"),
            )
            return {
                "result": {
                    "available_slots": [
                        {"date": s.date, "time": s.time, "provider": s.provider}
                        for s in slots
                    ]
                }
            }
        logger.info("Unhandled function_call: %s", fn)
        return {"result": {"error": f"Unsupported function: {fn}"}}

    logger.info("Unhandled Retell event: %s", event)
    return {"ok": True}
