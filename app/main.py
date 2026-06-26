"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    billing,
    bookings,
    callbacks,
    calls,
    dashboard,
    knowledge_base,
    me,
    onboarding,
    patients,
    practice,
    team,
    voice,
    waitlist,
)
from app.security import verify_security_config
from app.services.call_sync import call_sync_loop
from app.services.maintenance import maintenance_loop
from app.services.reactivation.worker import reactivation_worker_loop
from app.services.reminders import reminder_loop
from app.webhooks import clerk, retell, stripe, twilio_sms

_log_settings = get_settings()
setup_logging(_log_settings.log_level, json_logs=_log_settings.json_logs)
# Initialise Sentry BEFORE the app/middleware so its integrations hook in.
init_sentry(_log_settings)
logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start/stop background workers alongside the app."""
    settings = get_settings()
    verify_security_config(settings)
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




app = FastAPI(title="Dentiva Backend", version="0.1.0", lifespan=lifespan)
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
app.include_router(dashboard.router)
app.include_router(waitlist.router)
app.include_router(voice.router)
app.include_router(retell.router)
app.include_router(twilio_sms.router)
app.include_router(clerk.router)
app.include_router(stripe.router)
app.include_router(knowledge_base.router)
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
# retell-llm). Mounted only when explicitly enabled, so an unauthenticated WS
# isn't exposed by default (Security Sprint M7).
if get_settings().enable_llm_relay:
    logger.info("Groq LLM relay mounted (ENABLE_LLM_RELAY=true)")
