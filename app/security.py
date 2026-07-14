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
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

logger = logging.getLogger(__name__)

# Loopback hosts that must never appear in a production CORS allow-list.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local_origin(origin: str) -> bool:
    """True if the origin's HOST is a loopback address. Parses the host rather
    than substring-matching 'localhost' so a legit domain like
    'https://localhost.example.com' isn't wrongly rejected."""
    host = (urlparse(origin).hostname or "").lower()
    return host in _LOCAL_HOSTS


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
    - TWILIO_VALIDATE_SIGNATURE False / no token   → forgeable inbound SMS
    - RATE_LIMIT_ENABLED False in production        → no abuse backstop
    - CORS_ALLOWED_ORIGINS empty/localhost in prod  → browser origin not locked
    - STRIPE_SECRET_KEY set but no webhook secret   → forgeable billing events

    Soft warnings (logged, startup continues):
    - REMINDERS_ENABLED not set
    - TWILIO_VALIDATE_SIGNATURE not set or False

    NOTE: the DB-role check (app role must not be SUPERUSER/BYPASSRLS, or RLS FORCE
    is silently defeated) needs a live connection and lives in the async
    :func:`verify_db_security` below, called right after this from the lifespan.
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

        # Rate limiting is the abuse backstop for the OUTBOUND-money endpoints
        # (campaign launch fans out into TCPA-metered calls/SMS). It must not be an
        # "oops we forgot the env var" — make it a deploy gate, not a soft default.
        if not settings.rate_limit_enabled:
            raise RuntimeError(
                "SECURITY VIOLATION: RATE_LIMIT_ENABLED is False in production. "
                "Rate limiting is a required prod gate (a bug or stolen token could "
                "otherwise fan out into thousands of billable/TCPA calls). Set "
                "RATE_LIMIT_ENABLED=true in Railway."
            )

        # CORS with credentials must be pinned to the real dashboard origin(s). An
        # empty list or a leftover localhost origin in prod means the browser origin
        # allow-list isn't actually locked to our app.
        origins = settings.cors_origins_list
        bad_origins = [o for o in origins if _is_local_origin(o)]
        if not origins or bad_origins:
            raise RuntimeError(
                "SECURITY VIOLATION: CORS_ALLOWED_ORIGINS is empty or contains a "
                f"localhost origin in production ({bad_origins or 'empty'}). Set it to "
                "the exact dashboard domain(s), e.g. https://app.dentovox.com."
            )

        # If Stripe is live (secret key present), the webhook signature secret must
        # be set — otherwise a forged webhook could flip subscription state / grant
        # access. The /webhooks/stripe route already rejects when unset, but fail at
        # boot so a live-billing deploy can't go out half-configured.
        if settings.stripe_secret_key and not settings.stripe_webhook_secret:
            raise RuntimeError(
                "SECURITY VIOLATION: STRIPE_SECRET_KEY is set (live billing) but "
                "STRIPE_WEBHOOK_SECRET is empty in production — billing webhooks would "
                "be unauthenticated (forgeable subscription changes). Set "
                "STRIPE_WEBHOOK_SECRET in Railway."
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


async def verify_db_security(session: AsyncSession, settings: Settings) -> None:
    """Verify the app's DB role can't bypass Row-Level Security in production.

    RLS FORCE is our DB-level tenant-isolation backstop. But a SUPERUSER or a role
    with BYPASSRLS is EXEMPT from every policy — if the app connects as such, one
    clinic's PHI can leak to another and the whole RLS layer is silently a no-op.
    The app is supposed to connect as ``dentiva_app`` (NOSUPERUSER NOBYPASSRLS);
    this asserts that at boot so a misconfigured DATABASE_URL (e.g. pointing at the
    superuser role) fails the deploy instead of running unprotected.

    No-ops outside production (tests/dev connect as the owner for convenience).
    """
    if settings.environment != "production":
        return
    # current_user = the effective role executing queries (the app never SET ROLEs,
    # so it's the login role, dentiva_app). Checking THIS role's rolsuper/rolbypassrls
    # is exactly right: in Postgres, SUPERUSER and BYPASSRLS are role ATTRIBUTES that
    # are NOT inherited through role membership (only GRANTed privileges inherit), and
    # RLS bypass is decided by the current role's own attributes. So a NOSUPERUSER
    # NOBYPASSRLS role that merely belongs to a privileged group does NOT bypass RLS —
    # no membership cascade to chase here.
    row = (
        await session.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
    ).first()
    if row is None:
        logger.warning("verify_db_security: current_user not found in pg_roles — skipping")
        return
    is_super, bypass_rls = bool(row[0]), bool(row[1])
    if is_super or bypass_rls:
        flag = "SUPERUSER" if is_super else "BYPASSRLS"
        if settings.allow_superuser_db:
            # Pilot escape: acknowledged + no live clinics yet. Loud warning so it
            # is never forgotten — RLS FORCE is bypassed while this is set.
            logger.warning(
                "⚠️  DB role is %s and ALLOW_SUPERUSER_DB is set — RLS FORCE is "
                "BYPASSED. Acceptable ONLY pre-launch (no real patient PHI). Create "
                "the unprivileged 'dentiva_app' role and unset this before any live "
                "clinic (done as part of the AWS RDS migration).",
                flag,
            )
            return
        raise RuntimeError(
            f"SECURITY VIOLATION: the app's DB role is {flag} in production — it is "
            "EXEMPT from Row-Level Security, so tenant isolation (one clinic's PHI "
            "vs another's) is NOT enforced. Point DATABASE_URL at the unprivileged "
            "'dentiva_app' role (NOSUPERUSER NOBYPASSRLS). For a pre-launch pilot "
            "with no live clinics, set ALLOW_SUPERUSER_DB=true to boot anyway "
            "(RLS bypassed — must be undone before real patients)."
        )
    logger.info("DB security check passed (app role is non-superuser, non-bypassrls).")


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
