"""Clerk JWT verification via JWKS.

Verifies RS256 tokens against the practice's Clerk instance JWKS (cached 1h).
Extracts ``sub`` (clerk_user_id) and the org id claim (clerk_org_id).

For local testing without a live Clerk instance, ``AUTH_DEV_BYPASS=true`` plus
an ``X-Dev-*`` header set of claims is honored. Never enable in production.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException, Request, status
from jose import jwt
from jose.exceptions import JWTError

from app.config import get_settings

_JWKS_TTL_SECONDS = 3600
_jwks_keys: dict | None = None
_jwks_fetched_at: float = 0.0
# Cache is keyed by the URL it was fetched from. When CLERK_JWKS_URL changes
# (e.g. dev→prod Clerk instance), a stale cache must NOT be served — otherwise
# every prod token fails "Signing key not found" until the TTL expires.
_jwks_url: str | None = None


@dataclass
class AuthClaims:
    clerk_user_id: str
    clerk_org_id: str | None
    # Best-effort extras from the JWT (may be absent depending on Clerk token
    # template). Used only by lazy provisioning to pick a sensible initial role
    # and email; never trusted for authorization (that comes from our DB).
    clerk_org_role: str | None = None
    email: str | None = None


def _extract_org_role(claims: dict) -> str | None:
    # Clerk may expose the active org role as ``org_role`` or nested ``o.rol``.
    if "org_role" in claims:
        return claims["org_role"]
    org = claims.get("o")
    if isinstance(org, dict):
        return org.get("rol")
    return None


async def _get_jwks(jwks_url: str, *, force: bool = False) -> dict:
    global _jwks_keys, _jwks_fetched_at, _jwks_url
    now = time.time()
    fresh = (
        _jwks_keys is not None
        and _jwks_url == jwks_url
        and now - _jwks_fetched_at < _JWKS_TTL_SECONDS
    )
    if fresh and not force:
        return _jwks_keys
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        jwks = resp.json()
    _jwks_keys = jwks
    _jwks_fetched_at = now
    _jwks_url = jwks_url
    return jwks


def _find_signing_key(jwks: dict, kid: str | None) -> dict | None:
    return next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)


def _extract_org_id(claims: dict) -> str | None:
    # Clerk puts the active org in ``org_id``; some setups use ``o.id``.
    if "org_id" in claims:
        return claims["org_id"]
    org = claims.get("o")
    if isinstance(org, dict):
        return org.get("id")
    return None


async def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthClaims:
    """FastAPI dependency: verify the bearer token and return claims.

    Raises 401 on any auth failure.
    """
    settings = get_settings()

    # Dev bypass for local tests only — NEVER honored in production (defense in
    # depth; config also refuses to boot with this flag in production).
    if settings.auth_dev_bypass and settings.environment != "production":
        dev_user = request.headers.get("x-dev-clerk-user-id")
        dev_org = request.headers.get("x-dev-clerk-org-id")
        if dev_user:
            return AuthClaims(
                clerk_user_id=dev_user,
                clerk_org_id=dev_org,
                clerk_org_role=request.headers.get("x-dev-clerk-org-role"),
                email=request.headers.get("x-dev-clerk-email"),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Dev-Clerk-User-Id header (dev bypass).",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    token = authorization.split(" ", 1)[1].strip()

    if not settings.clerk_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth not configured (CLERK_JWKS_URL missing).",
        )

    try:
        jwks = await _get_jwks(settings.clerk_jwks_url)
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        key = _find_signing_key(jwks, kid)
        if key is None:
            # The kid isn't in our cached keys. This happens on legitimate Clerk
            # key rotation and right after a dev→prod instance switch. Force one
            # refresh before failing, so a new signing key doesn't 401 every
            # request until the TTL expires.
            jwks = await _get_jwks(settings.clerk_jwks_url, force=True)
            key = _find_signing_key(jwks, kid)
        if key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Signing key not found.",
            )
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except HTTPException:
        raise
    except (JWTError, httpx.HTTPError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        ) from exc

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
        )
    return AuthClaims(
        clerk_user_id=sub,
        clerk_org_id=_extract_org_id(claims),
        clerk_org_role=_extract_org_role(claims),
        email=claims.get("email"),
    )
