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
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import retell_llm_relay
from app.config import get_settings
from app.routes import (
    billing,
    bookings,
    callbacks,
    calls,
    dashboard,
    me,
    onboarding,
    patients,
    practice,
    team,
    voice,
    waitlist,
)
from app.services.call_sync import call_sync_loop
from app.services.reminders import reminder_loop
from app.webhooks import clerk, retell, stripe, twilio_sms

logging.basicConfig(level=get_settings().log_level.upper())
logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start/stop background workers alongside the app."""
    settings = get_settings()
    tasks: list[asyncio.Task] = []
    if settings.call_sync_enabled and settings.retell_api_key:
        tasks.append(asyncio.create_task(call_sync_loop()))
        logger.info("Retell call-sync background task started")
    elif settings.call_sync_enabled:
        logger.warning("CALL_SYNC_ENABLED but RETELL_API_KEY missing — sync disabled")

    if settings.reminders_enabled:
        tasks.append(asyncio.create_task(reminder_loop()))
        logger.info("Appointment-reminder background task started")

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Dentiva Backend", version="0.1.0", lifespan=lifespan)

# CORS — allow local dev and any Vercel deployment (including preview URLs).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://*.vercel.app",
        # Add your specific production domain here, e.g.:
        # "https://dentiva.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
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
# Groq custom-LLM relay (/ws/retell-llm) — NOT used by the live agent (native
# retell-llm). Mounted only when explicitly enabled, so an unauthenticated WS
# isn't exposed by default (Security Sprint M7).
if get_settings().enable_llm_relay:
    app.include_router(retell_llm_relay.router)
    logger.info("Groq LLM relay mounted (ENABLE_LLM_RELAY=true)")
