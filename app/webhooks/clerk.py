"""Clerk webhook handler (Platform Iter 1, Phase B1).

Receives Clerk events (svix-signed) and provisions our DB accordingly:
  user.created · organization.created · organizationMembership.created · user.deleted

SECURITY:
  * Signature is verified with the svix scheme against CLERK_WEBHOOK_SECRET.
  * When the secret is unset: allowed in dev (logged loudly), REJECTED in
    production — an unverified webhook could create/disable users, so we fail
    closed there. Mirrors the documented Retell-webhook policy but stricter,
    because this endpoint mutates identity.
  * Unknown event types are acknowledged (200) and ignored, so Clerk doesn't
    retry forever for events we don't model.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status

import app.db as _app_db
from app.config import get_settings
from app.services.clerk_provisioning import EVENT_HANDLERS
from app.webhooks.svix_verify import SvixVerificationError, verify

logger = logging.getLogger("dentiva.webhooks.clerk")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify(raw_body: bytes, svix_id, svix_timestamp, svix_signature) -> None:
    """Verify the svix signature, or raise HTTPException(401/403)."""
    settings = get_settings()
    secret = settings.clerk_webhook_secret
    if not secret:
        if settings.environment == "production":
            # Fail closed: never provision/disable identity from an unverified
            # source in production.
            logger.error("CLERK_WEBHOOK_SECRET unset in production — rejecting webhook.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Webhook verification not configured.",
            )
        logger.warning("CLERK_WEBHOOK_SECRET empty — skipping svix check (dev only).")
        return
    try:
        verify(
            secret=secret,
            raw_body=raw_body,
            svix_id=svix_id,
            svix_timestamp=svix_timestamp,
            svix_signature=svix_signature,
        )
    except SvixVerificationError as exc:
        # Don't leak which check failed beyond a generic message.
        logger.warning("clerk webhook signature rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        ) from exc


@router.post("/clerk", status_code=status.HTTP_200_OK)
async def clerk_webhook(
    request: Request,
    svix_id: str | None = Header(default=None, alias="svix-id"),
    svix_timestamp: str | None = Header(default=None, alias="svix-timestamp"),
    svix_signature: str | None = Header(default=None, alias="svix-signature"),
) -> dict:
    raw_body = await request.body()
    _verify(raw_body, svix_id, svix_timestamp, svix_signature)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc

    event_type = payload.get("type")
    data = payload.get("data") or {}
    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        # Acknowledge so Clerk stops retrying events we don't model.
        logger.info("clerk webhook: ignoring unmodeled event '%s'", event_type)
        return {"status": "ignored", "type": event_type}

    async with _app_db.async_session_factory() as session:
        try:
            result = await handler(session, data)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("clerk webhook handler failed for '%s'", event_type)
            # 500 → Clerk will retry; handlers are idempotent so retries are safe.
            raise HTTPException(
                status_code=500, detail="Webhook processing failed."
            ) from None

    logger.info("clerk webhook '%s' → %s", event_type, result)
    return {"status": "ok", "type": event_type, "result": result}
