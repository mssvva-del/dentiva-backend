"""
Retell webhook signature validation dependency.

Security model
--------------
Retell signs every POST /webhooks/retell request with HMAC-SHA256:

    signature = HMAC-SHA256(RETELL_WEBHOOK_SECRET, raw_body_bytes).hexdigest()
    Header:    X-Retell-Signature: <signature>

This dependency validates that signature **before** the route handler reads
the body.  It uses ``hmac.compare_digest`` for a constant-time comparison
that is immune to timing-oracle attacks.

Dev/backward-compat mode
------------------------
If ``RETELL_WEBHOOK_SECRET`` is an empty string or None, validation is
**skipped** with a one-time warning.  This lets the app start locally
without secrets configured.  In production the Railway variable MUST be set.

Body re-injection
-----------------
FastAPI / Starlette cache the raw body on ``request._body`` after the first
read.  We set that attribute directly so that downstream route handlers can
still call ``await request.body()`` or ``await request.json()`` normally.

Usage
-----

    @router.post("/webhooks/retell")
    async def retell_webhook(
        request: Request,
        _: None = Depends(verify_retell_signature),
    ):
        payload = await request.json()
        ...
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Depends, HTTPException, Request

# ---------------------------------------------------------------------------
# Lazy import of Settings / get_settings to avoid circular imports.
# The real app has these in app/config.py — adjust the import path if needed.
# ---------------------------------------------------------------------------
try:
    from app.config import Settings, get_settings  # type: ignore[import]
except ImportError:  # pragma: no cover — only during standalone testing
    from typing import Any  # noqa: F401

    Settings = Any  # type: ignore[assignment,misc]

    def get_settings() -> Any:  # type: ignore[return-value]
        raise RuntimeError("app.config not available")


logger = logging.getLogger(__name__)

# Emitted at most once per process lifetime so that dev-mode spam is avoided.
_dev_mode_warned: bool = False

# Header name sent by Retell on every webhook POST.
_RETELL_SIGNATURE_HEADER = "X-Retell-Signature"


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


async def verify_retell_signature(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """
    FastAPI dependency — validates the Retell HMAC-SHA256 webhook signature.

    Raises
    ------
    HTTPException(403)
        If the secret is configured but the signature is missing or invalid.

    Returns
    -------
    None
        On success (signature valid, or secret not configured).
    """
    global _dev_mode_warned  # noqa: PLW0603

    secret: str | None = getattr(settings, "retell_webhook_secret", None)

    # ------------------------------------------------------------------
    # Dev / backward-compat mode: skip validation when secret is absent.
    # ------------------------------------------------------------------
    if not secret:
        if not _dev_mode_warned:
            logger.warning(
                "RETELL_WEBHOOK_SECRET is not configured — webhook signature "
                "validation is DISABLED.  Set this variable in production."
            )
            _dev_mode_warned = True
        return

    # ------------------------------------------------------------------
    # Read raw body.  Starlette caches it on request._body so downstream
    # handlers can still call await request.body() / request.json().
    # ------------------------------------------------------------------
    body: bytes = await request.body()
    # Ensure the body is cached for downstream handlers (Starlette ≥ 0.20
    # sets _body automatically after the first read, but be explicit).
    request._body = body  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Retrieve signature from header.
    # ------------------------------------------------------------------
    received_sig: str | None = request.headers.get(_RETELL_SIGNATURE_HEADER)
    if not received_sig:
        _log_failure(request, reason="missing_header")
        raise HTTPException(
            status_code=403,
            detail="invalid_webhook_signature",
        )

    # ------------------------------------------------------------------
    # Compute expected HMAC-SHA256 signature.
    # ------------------------------------------------------------------
    expected_sig: str = hmac.new(
        key=secret.encode(),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # ------------------------------------------------------------------
    # Constant-time comparison — prevents timing oracle attacks.
    # ------------------------------------------------------------------
    if not hmac.compare_digest(expected_sig, received_sig):
        _log_failure(request, reason="signature_mismatch")
        raise HTTPException(
            status_code=403,
            detail="invalid_webhook_signature",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """
    Return the best available client IP from the request.

    Checks X-Forwarded-For first (set by Railway / reverse proxies), then
    falls back to the direct connection address.  We log the IP for
    forensics but never log the request body to protect PHI.
    """
    forwarded_for: str | None = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For may contain a comma-separated chain; the leftmost
        # entry is the original client IP.
        return forwarded_for.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _log_failure(request: Request, *, reason: str) -> None:
    """Log a failed signature validation attempt without exposing body content."""
    logger.warning(
        "Retell webhook signature validation failed | reason=%s ip=%s path=%s",
        reason,
        _client_ip(request),
        request.url.path,
    )
