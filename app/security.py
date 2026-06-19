"""
Dentiva — Security hardening module.

Two responsibilities:
  1. verify_security_config() — startup guard, called from lifespan() before
     the app accepts any traffic.
  2. check_emergency_lock()   — async gate in book_appointment tool handler;
     blocks booking when the active call is in emergency state.

Integration
-----------
See _sprint_security/APPLY.md for exact wiring instructions.
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
    - RETELL_WEBHOOK_SECRET missing in production  → forgeable webhooks
    - CLERK_SECRET_KEY missing in production       → broken auth
    - ENCRYPTION_KEY missing in production         → PHI stored in plaintext

    Soft warnings (logged, startup continues):
    - REMINDERS_ENABLED not set
    - TWILIO_VALIDATE_SIGNATURE not set or False
    """
    is_production = getattr(settings, "ENVIRONMENT", "development") == "production"

    # ── Hard failures ────────────────────────────────────────────────────────

    if getattr(settings, "AUTH_DEV_BYPASS", False) and is_production:
        raise RuntimeError(
            "SECURITY VIOLATION: AUTH_DEV_BYPASS=True while ENVIRONMENT='production'. "
            "This flag disables Clerk JWT verification entirely. "
            "Remove AUTH_DEV_BYPASS from Railway Variables before redeploying."
        )

    if is_production:
        required: dict[str, str | None] = {
            "RETELL_WEBHOOK_SECRET": getattr(settings, "RETELL_WEBHOOK_SECRET", None),
            "CLERK_SECRET_KEY":      getattr(settings, "CLERK_SECRET_KEY", None),
            "ENCRYPTION_KEY":        getattr(settings, "ENCRYPTION_KEY", None),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                f"SECURITY VIOLATION: Missing required production secrets: "
                f"{', '.join(missing)}. Set them in Railway → dentiva-backend → Variables."
            )

    # ── Soft warnings ────────────────────────────────────────────────────────

    if getattr(settings, "REMINDERS_ENABLED", None) is None:
        logger.warning(
            "CONFIG: REMINDERS_ENABLED is not set — reminder SMS disabled by default. "
            "Set REMINDERS_ENABLED=true on Railway to enable."
        )

    if not getattr(settings, "TWILIO_VALIDATE_SIGNATURE", False):
        logger.warning(
            "CONFIG: TWILIO_VALIDATE_SIGNATURE is False — Twilio webhook requests "
            "are NOT cryptographically verified. Set TWILIO_VALIDATE_SIGNATURE=true "
            "in production (after confirming your public webhook URL in Twilio Console)."
        )

    logger.info(
        "Security config check passed (ENVIRONMENT=%s, AUTH_DEV_BYPASS=%s).",
        getattr(settings, "ENVIRONMENT", "development"),
        getattr(settings, "AUTH_DEV_BYPASS", False),
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
