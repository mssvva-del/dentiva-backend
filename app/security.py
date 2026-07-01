"""
Dentiva — Security hardening module.

Two responsibilities:
  1. verify_security_config() — startup guard, called from lifespan() before
     the app accepts any traffic.
  2. check_emergency_lock()   — async gate in book_appointment tool handler;
     blocks booking when the active call is in emergency state.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PIECE 1 — Startup security guard
# ─────────────────────────────────────────────────────────────────────────────

def verify_security_config(settings: Settings) -> None:
    """
    Validate security-critical config before the app accepts traffic.

    Call this at the top of the lifespan() startup block in app/main.py.
    Raises RuntimeError on critical misconfiguration so Railway will mark
    the deploy as failed and surface the message in deployment logs.

    Hard failures (raises RuntimeError):
    - AUTH_DEV_BYPASS=True in production           → full auth bypass
    - DEMO_OPEN_ACCESS=True in production          → all users see same clinic
    - RETELL_WEBHOOK_SECRET missing in production  → forgeable webhooks
    - CLERK_SECRET_KEY missing in production       → broken auth
    - ENCRYPTION_KEY missing in production         → PHI stored in plaintext

    Soft warnings (logged, startup continues):
    - REMINDERS_ENABLED not set
    - TWILIO_VALIDATE_SIGNATURE not set or False
    """
    # Use direct attribute access (lowercase) — Settings uses pydantic_settings
    # which normalises all field names to lowercase. getattr(settings, "UPPER")
    # silently returns the default and the check never fires.
    is_production = settings.environment == "production"

    # ── Hard failures ────────────────────────────────────────────────────────

    if settings.auth_dev_bypass and is_production:
        raise RuntimeError(
            "SECURITY VIOLATION: AUTH_DEV_BYPASS=True while ENVIRONMENT='production'. "
            "This flag disables Clerk JWT verification entirely. "
            "Remove AUTH_DEV_BYPASS from Railway Variables before redeploying."
        )

    if settings.demo_open_access and is_production:
        raise RuntimeError(
            "SECURITY VIOLATION: DEMO_OPEN_ACCESS=True while ENVIRONMENT='production'. "
            "This flag attaches every authenticated user to the first practice in the DB. "
            "Set DEMO_OPEN_ACCESS=false in Railway Variables before redeploying."
        )

    if is_production:
        required: dict[str, str] = {
            "RETELL_WEBHOOK_SECRET": settings.retell_webhook_secret,
            "CLERK_SECRET_KEY":      settings.clerk_secret_key,
            "ENCRYPTION_KEY":        settings.encryption_key,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                f"SECURITY VIOLATION: Missing required production secrets: "
                f"{', '.join(missing)}. Set them in Railway → dentiva-backend → Variables."
            )

        # The inbound Twilio SMS webhook is ALWAYS mounted and acts on the caller's
        # From-number (CANCEL a booking, STOP-opt-out, waitlist backfill). Without
        # signature validation anyone who knows a patient's phone number can forge
        # these — so it must be verified in production, exactly like Retell/Clerk/
        # Stripe. Hard-fail rather than warn.
        if not settings.twilio_validate_signature:
            raise RuntimeError(
                "SECURITY VIOLATION: TWILIO_VALIDATE_SIGNATURE=False while "
                "ENVIRONMENT='production'. The inbound Twilio SMS webhook would be "
                "unauthenticated (a forged request could cancel a patient's appointment "
                "or opt them out). Set TWILIO_VALIDATE_SIGNATURE=true and TWILIO_AUTH_TOKEN "
                "in Railway."
            )
        # Validation without the token silently rejects EVERY inbound SMS (fail-closed
        # but a silent outage) — the token is required for signatures to verify at all.
        if not settings.twilio_auth_token:
            raise RuntimeError(
                "SECURITY VIOLATION: TWILIO_VALIDATE_SIGNATURE=True but TWILIO_AUTH_TOKEN "
                "is empty in production — signature verification would reject every "
                "inbound SMS (CONFIRM/CANCEL/STOP silently dropped). Set TWILIO_AUTH_TOKEN."
            )

    # ── Soft warnings ────────────────────────────────────────────────────────

    if not settings.reminders_enabled:
        logger.warning(
            "CONFIG: REMINDERS_ENABLED is not set — reminder SMS disabled by default. "
            "Set REMINDERS_ENABLED=true on Railway to enable."
        )

    if not is_production and not settings.twilio_validate_signature:
        logger.warning(
            "CONFIG: TWILIO_VALIDATE_SIGNATURE is False — Twilio webhook requests "
            "are NOT cryptographically verified (dev). Required in production."
        )

    logger.info(
        "Security config check passed (ENVIRONMENT=%s, AUTH_DEV_BYPASS=%s, DEMO_OPEN_ACCESS=%s).",
        settings.environment,
        settings.auth_dev_bypass,
        settings.demo_open_access,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PIECE 2 — Emergency lock gate for book_appointment
# ─────────────────────────────────────────────────────────────────────────────

async def check_emergency_lock(call_id: str, session: AsyncSession) -> None:
    """
    Block booking if the active call has emergency_active = True.

    Must be awaited BEFORE any booking logic in the book_appointment handler.
    Single lightweight SELECT — no joins, no ORM overhead.

    Retell treats non-2xx as a failed tool call and surfaces it to the
    agent. 409 is appropriate: "the resource is locked due to a conflicting
    state" (emergency in progress).

    Args:
        call_id: Retell call identifier from the webhook payload.
        session: Active AsyncSession from FastAPI dependency injection.

    Raises:
        HTTPException(409): when emergency_active is True for this call.
    """
    result = await session.execute(
        text(
            "SELECT emergency_active FROM calls "
            "WHERE retell_call_id = :call_id LIMIT 1"
        ),
        {"call_id": call_id},
    )
    row = result.fetchone()

    if row is None:
        # Race: call_started event not yet written, or unknown call_id.
        # Do NOT block — silently dropping a real booking is worse than
        # this edge case. Log so ops can investigate if it recurs.
        logger.warning(
            "check_emergency_lock: call_id=%r not in calls table — "
            "allowing booking to proceed. Investigate if this recurs.",
            call_id,
        )
        return

    if row[0]:  # emergency_active is True
        logger.warning(
            "check_emergency_lock: BOOKING BLOCKED — call_id=%r has "
            "emergency_active=True. Returning 409 to Retell.",
            call_id,
        )
        raise HTTPException(
            status_code=409,
            detail="booking_blocked_emergency",
        )
