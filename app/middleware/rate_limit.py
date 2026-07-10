"""
Rate limiting middleware for Dentiva FastAPI backend.

Uses SlowAPI (a Starlette/FastAPI wrapper around limits + slowapi).

Strategy
--------
- Authenticated API endpoints  : 120 req/min per Clerk user_id (fallback to IP)
- Webhook endpoints             : 60 req/min per IP
- Voice endpoints               : 10 req/min per IP
- All other endpoints           : 120 req/min per IP (global default)

Usage in routers
----------------
    from app.middleware.rate_limit import limiter

    @router.get("/some-path")
    @limiter.limit("120/minute")
    async def handler(request: Request):
        ...

The `request: Request` parameter MUST be present in every decorated handler
because SlowAPI reads the request object to resolve the rate-limit key.
"""

from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from limits.storage import MemoryStorage
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key functions
# ---------------------------------------------------------------------------


def _extract_clerk_user_id(request: Request) -> str | None:
    """
    Pull the Clerk `sub` claim from an already-decoded JWT payload.

    The Clerk JWT middleware (or a dependency) is expected to stash the
    decoded claims on ``request.state.user_claims`` as a dict.  If that
    attribute is absent (unauthenticated / webhook path), returns None.
    """
    try:
        claims: dict = getattr(request.state, "user_claims", None)
        if claims and isinstance(claims, dict):
            return claims.get("sub")
    except Exception:  # noqa: BLE001
        pass
    return None


def _key_for_authenticated_endpoint(request: Request) -> str:
    """
    Rate-limit key: Clerk user_id when available, IP address otherwise.

    This prevents a single authenticated user from exhausting the limit
    across multiple IPs (e.g. a client cycling through proxies) and also
    provides a sensible fallback for endpoints that accept both
    authenticated and anonymous traffic.
    """
    user_id = _extract_clerk_user_id(request)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{get_remote_address(request)}"


def _key_for_ip(request: Request) -> str:
    """Plain IP-based key used for webhooks and voice endpoints."""
    return f"ip:{get_remote_address(request)}"


# ---------------------------------------------------------------------------
# Limiter instance
# ---------------------------------------------------------------------------

# MemoryStorage is appropriate for a single-process Railway deployment.
# For multi-process / multi-instance deployments, swap to RedisStorage:
#
#   from limits.storage import RedisStorage
#   storage = RedisStorage("redis://your-redis-url")
#
# and update the Limiter instantiation below.

_storage = MemoryStorage()

limiter = Limiter(
    key_func=_key_for_authenticated_endpoint,
    default_limits=["120/minute"],
    headers_enabled=True,
    swallow_errors=False,
    enabled=True,
)

# ---------------------------------------------------------------------------
# Per-path key-function helpers exposed for use in routers
# ---------------------------------------------------------------------------

#: Use on @limiter.limit(..., key_func=authenticated_key)
authenticated_key = _key_for_authenticated_endpoint

#: Use on @limiter.limit(..., key_func=ip_key)
ip_key = _key_for_ip


# ---------------------------------------------------------------------------
# Pre-configured limit decorators (convenience wrappers)
# ---------------------------------------------------------------------------

def limit_authenticated(limit_string: str = "120/minute"):
    """
    Decorator for authenticated API endpoints.

    Example::

        @router.get("/patients")
        @limit_authenticated()
        async def list_patients(request: Request, ...):
            ...
    """
    return limiter.limit(limit_string, key_func=_key_for_authenticated_endpoint)


def limit_webhook(limit_string: str = "60/minute"):
    """
    Decorator for webhook endpoints (/webhooks/*).

    Signature validation is the primary security control; this limit is a
    backstop against runaway callers or misconfigured upstream services.

    Example::

        @router.post("/webhooks/retell")
        @limit_webhook()
        async def retell_webhook(request: Request, ...):
            ...
    """
    return limiter.limit(limit_string, key_func=_key_for_ip)


def limit_public(limit_string: str = "20/minute"):
    """Per-IP limit for UNAUTHENTICATED public endpoints (e.g. the marketing-site
    lead form). Caps spam/abuse on an endpoint anyone can POST to."""
    return limiter.limit(limit_string, key_func=_key_for_ip)


def limit_voice(limit_string: str = "10/minute"):
    """
    Decorator for voice/AI endpoints (/api/voice/*).

    Voice calls are expensive (Retell + LLM tokens).  A conservative per-IP
    limit reduces abuse surface area.

    Example::

        @router.post("/api/voice/initiate")
        @limit_voice()
        async def initiate_voice_call(request: Request, ...):
            ...
    """
    return limiter.limit(limit_string, key_func=_key_for_ip)


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """
    Return a structured JSON 429 response instead of SlowAPI's plain-text default.

    Registered via::

        app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    """
    limit_str = getattr(exc, "limit", None)
    retry_after: str | None = None

    # SlowAPI attaches response headers to the exception; surface Retry-After.
    if hasattr(exc, "retry_after"):
        retry_after = str(exc.retry_after)

    logger.warning(
        "Rate limit exceeded | path=%s method=%s key=%s limit=%s",
        request.url.path,
        request.method,
        exc.detail if hasattr(exc, "detail") else "unknown",
        limit_str,
    )

    headers = {"Retry-After": retry_after} if retry_after else {}

    # Match the app-wide error envelope (app.main._error_payload): the client only
    # knows how to read error.message, so a bespoke {"error": "<string>"} shape here
    # would surface as a blank/garbled message to the operator. Keep it identical.
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": "Too many requests. Please slow down and try again later.",
                # str() — exc.limit is a slowapi Limit object, NOT JSON-serializable;
                # embedding it raw would make json.dumps raise and 500 the handler.
                "details": {"limit": str(limit_str)} if limit_str else {},
            }
        },
        headers=headers,
    )
