"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.logging_config import setup_logging
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.middleware.request_context import RequestContextMiddleware
from app.observability.sentry import init_sentry
from app.routes import (
    admin,
    assistant,
    billing,
    bookings,
    callbacks,
    calls,
    dashboard,
    knowledge_base,
    leads,
    me,
    onboarding,
    patients,
    practice,
    pricing,
    reactivation,
    team,
    voice,
    waitlist,
)
from app.security import verify_db_security, verify_security_config
from app.services.call_sync import call_sync_loop
from app.services.maintenance import maintenance_loop
from app.services.reactivation.worker import reactivation_worker_loop
from app.services.reminders import reminder_loop
from app.webhooks import clerk, retell, stripe, twilio_sms

_log_settings = get_settings()
setup_logging(_log_settings.log_level, json_logs=_log_settings.json_logs)
# Initialise Sentry BEFORE the app/middleware so its integrations hook in.
init_sentry(_log_settings)
# Railway sets this on every build; empty when running locally or from a tarball.
_REVISION = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "unknown")[:7]

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start/stop background workers alongside the app."""
    settings = get_settings()
    verify_security_config(settings)
    # DB-role check needs a live connection (verify the app role can't bypass RLS).
    # No-ops outside production; hard-fails the deploy if the app connects as a
    # superuser/BYPASSRLS role (which would defeat tenant isolation).
    import app.db as _app_db

    async with _app_db.async_session_factory() as _sec_session:
        # Bound the check so a hung/unreachable DB fails the deploy fast (fail-closed
        # in prod) instead of blocking startup forever. async-with still closes the
        # connection on timeout/error.
        await asyncio.wait_for(verify_db_security(_sec_session, settings), timeout=15)
    tasks: list[asyncio.Task] = []
    if settings.call_sync_enabled and settings.retell_api_key:
        tasks.append(asyncio.create_task(call_sync_loop()))
        logger.info("Retell call-sync background task started")
    elif settings.call_sync_enabled:
        logger.warning("CALL_SYNC_ENABLED but RETELL_API_KEY missing — sync disabled")

    if settings.reminders_enabled:
        tasks.append(asyncio.create_task(reminder_loop()))
        logger.info("Appointment-reminder background task started")

    if settings.maintenance_enabled:
        tasks.append(asyncio.create_task(maintenance_loop()))
        logger.info("Maintenance background task started")

    if settings.reactivation_enabled:
        tasks.append(asyncio.create_task(reactivation_worker_loop()))
        logger.info("Reactivation worker background task started")

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Close the pooled Groq client (no-op if the relay never ran).
        from app.services.llm.groq_client import aclose_pool

        await aclose_pool()




app = FastAPI(title="Dentovox Backend", version="0.1.0", lifespan=lifespan)
# WHY middleware order matters: Starlette runs middleware in REVERSE order of
# add_middleware() — last added is outermost (runs first on the way in). So the
# order below is deliberate: RequestContextMiddleware is added LAST so it wraps
# everything and a request_id exists before any other layer (incl. rate-limit
# rejections) can log. Adding it earlier would leave early-rejected requests
# without a correlation id.
# ── Rate limiting ────────────────────────────────────────────────
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
# ─────────────────────────────────────────────────────────────────


# CORS — exact allowed origins from CORS_ALLOWED_ORIGINS (comma-separated env).
# No wildcards/regex: credentials are sent, so origins must be explicit. Set the
# exact prod dashboard domain(s) in Railway; defaults to local dev.
_cors_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-IP rate limiting (Security Sprint H2) — gated, generous, /health exempt.
_rl_settings = get_settings()
if _rl_settings.rate_limit_enabled:
    from app.middleware.ratelimit import RateLimitMiddleware

    app.add_middleware(
        RateLimitMiddleware,
        per_minute=_rl_settings.rate_limit_per_minute,
        webhook_per_minute=_rl_settings.rate_limit_webhook_per_minute,
    )
    logger.info("Rate limiting enabled (%s/min, webhooks %s/min)",
                _rl_settings.rate_limit_per_minute,
                _rl_settings.rate_limit_webhook_per_minute)

# Outermost middleware: assign request_id before anything else runs, so every
# log line (including rate-limit rejections) is correlated and the response
# always carries X-Request-ID. Added last → wraps the whole stack.
app.add_middleware(RequestContextMiddleware)

# Map HTTP status -> stable error code for the unified envelope.
_STATUS_CODE_MAP = {
    400: "INVALID_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    429: "RATE_LIMITED",
    500: "SERVER_ERROR",
}


def _error_payload(status_code: int, message: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": _STATUS_CODE_MAP.get(status_code, "ERROR"),
            "message": message,
            "details": details or {},
        }
    }


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.status_code, str(exc.detail)),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # jsonable_encoder makes the error list JSON-safe: Pydantic v2 puts the raw
    # ValueError in each error's ``ctx`` for custom field_validators, and that
    # object is NOT directly JSON-serializable (would 500 the error handler).
    return JSONResponse(
        status_code=400,
        content=_error_payload(
            400, "Request validation failed", {"errors": jsonable_encoder(exc.errors())}
        ),
    )


@app.get("/health")
async def health() -> dict:
    """Liveness — process is up. No external dependencies touched."""
    return {"status": "ok"}


@app.get("/health/detailed")
async def health_detailed() -> JSONResponse:
    """At-a-glance operational health (NO PHI) — for an external uptime monitor
    and the ops dashboard. Reports DB reachability, whether the voice/SMS
    dependencies are configured, and how many critical alerts fired this hour.
    Returns 503 if the DB is down OR alerts are firing, so UptimeRobot pages."""
    from sqlalchemy import text as _text

    import app.db as app_db
    from app.observability.alerts import recent_alerts

    settings = get_settings()
    db_ok = True
    # Is the tenant boundary actually ON? RLS FORCE is our isolation backstop, and
    # a SUPERUSER/BYPASSRLS login role is exempt from every policy — the whole
    # layer becomes a silent no-op, with no error and no failing test to show for
    # it. verify_db_security refuses to boot in that state unless ALLOW_SUPERUSER_DB
    # is set, which is exactly the pilot escape hatch that is easy to leave on. So
    # report it continuously rather than only at boot.
    rls_enforced = None
    try:
        async with app_db.async_session_factory() as session:
            await session.execute(_text("SELECT 1"))
            row = (await session.execute(_text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))).first()
            if row is not None:
                rls_enforced = not (bool(row[0]) or bool(row[1]))
    except Exception:  # noqa: BLE001
        db_ok = False

    alerts = recent_alerts()
    body = {
        "status": "ok" if (db_ok and alerts["count_last_hour"] == 0) else "degraded",
        "db": "ok" if db_ok else "down",
        # Which commit is actually answering. Nine merged pull requests once sat
        # in main for a week while production served a build from before any of
        # them — two of those were security fixes. Nobody was careless: every
        # merge was followed by a health check that returned "ok", and "ok" means
        # the box is alive, not that the box is current. Without this field the
        # only way to notice was for a change to happen to be visible from
        # outside, which is luck, not verification.
        "revision": _REVISION,
        "voice_configured": bool(settings.retell_api_key and settings.retell_agent_id),
        "sms_enabled": bool(settings.sms_enabled and settings.twilio_auth_token),
        "webhook_verified": bool(settings.retell_webhook_secret),
        # None = couldn't determine (DB down / role not in pg_roles).
        "rls_enforced": rls_enforced,
        "alerts": alerts,
        # Which capabilities have credentials, so "what are we still missing?" is
        # answerable from outside the box. Booleans only — never a value, never a
        # prefix. Each one is a whole feature that is silently inert without it:
        # nobody can subscribe, no reactivation call dials, no PMS is reachable.
        "capabilities": {
            "take_payment": bool(
                settings.stripe_secret_key and settings.stripe_price_growth_monthly
            ),
            "billing_webhook": bool(settings.stripe_webhook_secret),
            "identity_webhook": bool(settings.clerk_webhook_secret),
            "outbound_calls": bool(
                settings.retell_outbound_agent_id and settings.retell_from_number
            ),
            "pms_nexhealth": bool(settings.nexhealth_api_key),
            "pms_open_dental": bool(settings.open_dental_dev_key),
            "call_review": bool(settings.anthropic_api_key),
            "error_tracking": bool(settings.sentry_dsn),
        },
    }
    code = 200 if body["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=body)


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Readiness — verifies the DB is reachable (SELECT 1)."""
    from sqlalchemy import text as _text

    import app.db as app_db

    try:
        async with app_db.async_session_factory() as session:
            await session.execute(_text("SELECT 1"))
        return JSONResponse(status_code=200, content={"status": "ready"})
    except Exception as exc:  # noqa: BLE001 — report not-ready, don't crash
        logger.warning("readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "detail": "db_unreachable"}
        )


app.include_router(assistant.router)
app.include_router(me.router)
app.include_router(onboarding.router)
app.include_router(team.router)
app.include_router(billing.router)
app.include_router(admin.router)
app.include_router(practice.router)
app.include_router(calls.router)
app.include_router(bookings.router)
app.include_router(callbacks.router)
app.include_router(patients.router)
app.include_router(leads.router)
app.include_router(pricing.router)
app.include_router(reactivation.router)
app.include_router(dashboard.router)
app.include_router(waitlist.router)
app.include_router(voice.router)
app.include_router(retell.router)
app.include_router(twilio_sms.router)
app.include_router(clerk.router)
app.include_router(stripe.router)
app.include_router(knowledge_base.router)

# Groq custom-LLM relay for Retell — mounted ONLY in NON-production and only when
# enabled. In production it processes live-call PHI without a BAA, so the startup
# guard (verify_security_config) hard-fails if the flag is on; this mount is a
# second line of defense (never mount it in prod even if the guard is bypassed).
if get_settings().enable_llm_relay and get_settings().environment != "production":
    from app.api.llm_relay import router as llm_relay_router

    app.include_router(llm_relay_router)
    logger.info("Groq LLM relay mounted at /ws/retell-llm (ENABLE_LLM_RELAY=true)")
# PLACEHOLDER — Recall / reactivation campaigns (see _docs/RECALL_CAMPAIGNS.md).
# When app/routes/recall.py lands, mount it here AND rate-limit the outbound
# trigger routes hard, e.g.:
#     app.include_router(recall.router)
#     # on POST /api/recall/campaigns/{id}/launch and /import:
#     @router.post(".../launch")
#     @limiter.limit("5/minute")
#     async def launch(...): ...
# WHY a tight limit specifically here: these endpoints fan out into OUTBOUND calls
# and SMS (real money + a federal TCPA/telephony footprint). A loose limit lets a
# bug or a compromised token dial thousands of patients before anyone notices —
# unlike read endpoints, the blast radius is external and irreversible. Keep the
# launch/import limit far stricter than the generic per-IP limit.
