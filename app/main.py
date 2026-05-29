"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import retell_llm_relay
from app.config import get_settings
from app.routes import bookings, calls, dashboard, practice
from app.webhooks import retell

logging.basicConfig(level=get_settings().log_level.upper())

app = FastAPI(title="Dentiva Backend", version="0.1.0")

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
    return JSONResponse(
        status_code=400,
        content=_error_payload(400, "Request validation failed", {"errors": exc.errors()}),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.include_router(practice.router)
app.include_router(calls.router)
app.include_router(bookings.router)
app.include_router(dashboard.router)
app.include_router(retell.router)
app.include_router(retell_llm_relay.router)
