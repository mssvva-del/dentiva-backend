"""
Twilio webhook signature validation dependency.

Security model
--------------
Twilio signs every incoming HTTP request with an HMAC-SHA1 signature computed
over the full request URL plus the sorted POST parameters:

    from twilio.request_validator import RequestValidator  # lazy import
    validator = RequestValidator(auth_token)
    valid = validator.validate(url, post_params, X-Twilio-Signature)

This dependency wraps that check as a FastAPI ``Depends``-compatible
async function.

Dev / backward-compat mode
--------------------------
If ``TWILIO_VALIDATE_SIGNATURE`` is ``False`` (the project default for local
development), validation is skipped with a one-time warning.  Flip it to
``True`` on Railway before enabling the Twilio SMS webhook in the Console.

URL reconstruction
------------------
Twilio computes its signature against the URL it originally POSTed to,
including any query string.  When the app sits behind a proxy (Railway,
ngrok), ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` are used to rebuild
the original public URL.  If those headers are absent the URL is taken from
the request as-is.

Body caching
------------
As with the Retell dependency, raw form-encoded bytes are read once and
stored on ``request._body`` so downstream handlers can still call
``await request.form()`` or ``await request.body()`` without double-read
issues.

Usage
-----
    from app.api.dependencies.twilio_auth import verify_twilio_signature

    @router.post("/webhooks/twilio/sms")
    async def twilio_sms_webhook(
        request: Request,
        _: None = Depends(verify_twilio_signature),
    ):
        form = await request.form()
        ...
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

try:
    from app.config import Settings, get_settings  # type: ignore[import]
except ImportError:  # pragma: no cover — only during standalone testing
    Settings = Any  # type: ignore[assignment,misc]

    def get_settings() -> Any:  # type: ignore[return-value]
        raise RuntimeError("app.config not available")


logger = logging.getLogger(__name__)

# Emitted at most once per process lifetime.
_dev_mode_warned: bool = False

_TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


async def verify_twilio_signature(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """
    FastAPI dependency — validates the Twilio HMAC-SHA1 webhook signature.

    Raises
    ------
    HTTPException(403)
        If validation is enabled but the signature is missing or invalid.

    Returns
    -------
    None
        On success (signature valid, or validation disabled).
    """
    global _dev_mode_warned  # noqa: PLW0603

    validate: bool = getattr(settings, "twilio_validate_signature", False)

    # ------------------------------------------------------------------
    # Dev / backward-compat mode: skip when validation is disabled.
    # ------------------------------------------------------------------
    if not validate:
        if not _dev_mode_warned:
            logger.warning(
                "TWILIO_VALIDATE_SIGNATURE is False — Twilio webhook signature "
                "validation is DISABLED.  Enable it in production by setting "
                "TWILIO_VALIDATE_SIGNATURE=true on Railway."
            )
            _dev_mode_warned = True
        return

    # ------------------------------------------------------------------
    # Retrieve required config values.
    # ------------------------------------------------------------------
    auth_token: Optional[str] = getattr(settings, "twilio_auth_token", None)
    if not auth_token:
        # Should not happen when validate=True, but guard defensively.
        logger.error(
            "TWILIO_VALIDATE_SIGNATURE=true but TWILIO_AUTH_TOKEN is not "
            "configured.  Cannot validate Twilio webhook signature."
        )
        raise HTTPException(
            status_code=500,
            detail="twilio_auth_token_not_configured",
        )

    # ------------------------------------------------------------------
    # Read and cache the raw body so downstream handlers are unaffected.
    # ------------------------------------------------------------------
    body: bytes = await request.body()
    request._body = body  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Retrieve the Twilio signature from the request headers.
    # ------------------------------------------------------------------
    twilio_sig: Optional[str] = request.headers.get(_TWILIO_SIGNATURE_HEADER)
    if not twilio_sig:
        _log_failure(request, reason="missing_header")
        raise HTTPException(
            status_code=403,
            detail="invalid_twilio_signature",
        )

    # ------------------------------------------------------------------
    # Reconstruct the public URL that Twilio signed.
    # Proxies (Railway, ngrok) forward the original scheme and host in
    # X-Forwarded-Proto / X-Forwarded-Host.
    # ------------------------------------------------------------------
    public_url: str = _reconstruct_public_url(request)

    # ------------------------------------------------------------------
    # Parse the form-encoded POST params that Twilio includes in its
    # signature computation.  An empty dict is fine for GET requests or
    # JSON bodies (Twilio's status callbacks are always form-encoded).
    # ------------------------------------------------------------------
    post_params: Dict[str, str] = {}
    content_type: str = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type and body:
        post_params = dict(urllib.parse.parse_qsl(body.decode("utf-8", errors="replace")))

    # ------------------------------------------------------------------
    # Validate using Twilio's official RequestValidator.
    # This internally computes HMAC-SHA1 over the URL + sorted params,
    # base64-encodes the digest, and does a constant-time comparison.
    # ------------------------------------------------------------------
    from twilio.request_validator import RequestValidator  # lazy import
    validator = RequestValidator(auth_token)
    is_valid: bool = validator.validate(
        uri=public_url,
        params=post_params,
        signature=twilio_sig,
    )

    if not is_valid:
        _log_failure(request, reason="signature_mismatch")
        raise HTTPException(
            status_code=403,
            detail="invalid_twilio_signature",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _reconstruct_public_url(request: Request) -> str:
    """
    Rebuild the public-facing URL that Twilio used when computing the signature.

    When the backend sits behind Railway's reverse proxy or ngrok, the URL
    in ``request.url`` reflects the internal address (http://0.0.0.0:8080/...).
    Twilio signs against the public URL it POSTed to, so we override the
    scheme and host from ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` when
    those headers are present.
    """
    forwarded_proto: Optional[str] = request.headers.get("X-Forwarded-Proto")
    forwarded_host: Optional[str] = request.headers.get("X-Forwarded-Host")

    url = request.url
    scheme: str = forwarded_proto.split(",")[0].strip() if forwarded_proto else url.scheme
    host: str = forwarded_host.split(",")[0].strip() if forwarded_host else url.netloc

    # Preserve path + query string exactly.
    path_qs: str = url.path
    if url.query:
        path_qs = f"{url.path}?{url.query}"

    return f"{scheme}://{host}{path_qs}"


def _client_ip(request: Request) -> str:
    """
    Return the best available client IP, checking X-Forwarded-For first.

    We log the IP for forensic purposes but never log the request body to
    protect PHI (Twilio form payloads often contain phone numbers).
    """
    forwarded_for: Optional[str] = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _log_failure(request: Request, *, reason: str) -> None:
    """Log a failed signature validation attempt without exposing body content."""
    logger.warning(
        "Twilio webhook signature validation failed | reason=%s ip=%s path=%s",
        reason,
        _client_ip(request),
        request.url.path,
    )
