"""Thin Clerk Backend API client for organization invitations (Phase B3).

Best-effort: when CLERK_SECRET_KEY is unset (local/dev/test) the calls no-op and
return None, so the team flow still works against our own DB. When a secret IS
set, we create/revoke the Clerk organization invitation so the invitee actually
receives an email and lands in the right org with our assigned role on signup.

Failures NEVER raise into the request path — our DB invitation row is the source
of truth for the UI; Clerk delivery is a side effect the owner can retry. We log
loudly so a misconfigured key is visible.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("dentiva.services.clerk_api")

_CLERK_API_BASE = "https://api.clerk.com/v1"


async def create_org_invitation(
    *, clerk_org_id: str, email: str, clerk_role: str
) -> str | None:
    """Create a Clerk organization invitation. Returns the Clerk invitation id.

    `clerk_role` is the Clerk role key (e.g. 'org:admin' / 'org:member'). Returns
    None when no secret is configured or the call fails (logged).
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        logger.info("CLERK_SECRET_KEY unset — skipping Clerk invitation (dev).")
        return None
    url = f"{_CLERK_API_BASE}/organizations/{clerk_org_id}/invitations"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
                json={"email_address": email, "role": clerk_role},
            )
        if resp.status_code >= 400:
            logger.warning(
                "Clerk invitation failed (%s): %s", resp.status_code, resp.text[:200]
            )
            return None
        return resp.json().get("id")
    except Exception:  # noqa: BLE001 — never break the request on a side effect
        logger.exception("Clerk invitation request errored")
        return None


async def create_organization(*, name: str) -> str | None:
    """Create a Clerk organization and return its id, or None.

    Identity lives in Clerk: a practice row is born when an organization exists,
    in both of the paths that create one. So a clinic created in the admin panel
    without an organization is an orphan — nobody can sign in to it, and when the
    real owner eventually creates their own organization, a SECOND practice
    appears alongside it.

    Which is why the caller must treat None as a refusal to create anything. A
    half-created clinic is worse than no button.
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        logger.info("CLERK_SECRET_KEY unset — cannot create a Clerk organization.")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_CLERK_API_BASE}/organizations",
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
                json={"name": name},
            )
        if resp.status_code >= 400:
            logger.warning(
                "Clerk organization create failed (%s): %s",
                resp.status_code, resp.text[:200],
            )
            return None
        return resp.json().get("id")
    except Exception:  # noqa: BLE001 — the caller refuses rather than half-creates
        logger.exception("Clerk organization create errored")
        return None


async def revoke_org_invitation(*, clerk_org_id: str, clerk_invitation_id: str) -> bool:
    """Revoke a Clerk org invitation. Returns True on success (best effort)."""
    settings = get_settings()
    if not settings.clerk_secret_key:
        return False
    url = (
        f"{_CLERK_API_BASE}/organizations/{clerk_org_id}"
        f"/invitations/{clerk_invitation_id}/revoke"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            )
        return resp.status_code < 400
    except Exception:  # noqa: BLE001
        logger.exception("Clerk invitation revoke errored")
        return False


def clinic_role_to_clerk_role(role: str) -> str:
    """Map our clinic role to the Clerk org role used for the invitation.

    Clerk's two built-in org roles are admin/member; owner → admin, everything
    else → member. Our DB role (set on the invitation) remains authoritative for
    authorization — Clerk's role only affects Clerk-side org membership UI.
    """
    return "org:admin" if role == "owner" else "org:member"


# ---------------------------------------------------------------------------
# Platform (instance-level) invitations — ADM9 internal-staff invites.
# ---------------------------------------------------------------------------


class ClerkError(Exception):
    """Clerk request failed where the caller NEEDS to know (admin staff invites).
    ``status_code`` lets the route map upstream 5xx/transport → 502, 4xx → 422."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def create_platform_invitation(
    *,
    email: str,
    public_metadata: dict | None = None,
    redirect_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Create an INSTANCE-level Clerk invitation (not org): Clerk emails the person
    a signup link; on signup the invitation's public_metadata is copied onto the
    user, so our user.created webhook reads the assigned dentiva_role and
    provisions the internal staff row automatically.

    Unlike the org-invitation helpers above this is NOT best-effort — the admin
    clicked "Invite" and must see an honest error, so failures raise ClerkError.
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        raise ClerkError("CLERK_SECRET_KEY not configured", status_code=503)
    body: dict = {"email_address": email}
    if public_metadata:
        body["public_metadata"] = public_metadata
    if redirect_url:
        body["redirect_url"] = redirect_url
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.post(
                f"{_CLERK_API_BASE}/invitations",
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ClerkError(
            f"Clerk unreachable: {type(exc).__name__}", status_code=502
        ) from exc
    if resp.status_code >= 400:
        try:
            msg = (resp.json().get("errors") or [{}])[0].get("message", "")[:200]
        except Exception:  # noqa: BLE001
            msg = resp.text[:200]
        raise ClerkError(f"Clerk {resp.status_code}: {msg}", status_code=resp.status_code)
    return resp.json().get("id", "")


async def find_clerk_user_by_email(
    email: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    """Clerk user id for an email, or None. Used by the staff-invite fallback:
    when Clerk rejects an invitation because the address already has an account,
    we look the user up and promote them directly instead of failing."""
    settings = get_settings()
    if not settings.clerk_secret_key:
        raise ClerkError("CLERK_SECRET_KEY not configured", status_code=503)
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.get(
                f"{_CLERK_API_BASE}/users",
                params={"email_address": [email], "limit": 10},
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            )
    except httpx.HTTPError as exc:
        raise ClerkError(
            f"Clerk unreachable: {type(exc).__name__}", status_code=502
        ) from exc
    if resp.status_code >= 400:
        raise ClerkError(f"Clerk {resp.status_code}", status_code=resp.status_code)
    data = resp.json()
    users = data if isinstance(data, list) else data.get("data", [])
    target = email.lower()
    for u in users:
        for ea in u.get("email_addresses", []):
            if ea.get("email_address", "").lower() == target:
                return u["id"]
    return users[0]["id"] if users else None


async def fetch_user_email(clerk_user_id: str) -> str | None:
    """The user's primary email, straight from Clerk.

    Needed because ``user.created`` can arrive before an address is attached,
    and we store ``<clerk_id>@unknown.clerk`` to satisfy the NOT NULL column.
    ``user.updated`` fixes that going forward, but a row already carrying the
    placeholder never hears another event — and the admin screen then shows a
    machine id where the practice owner's email belongs.

    Returns None when Clerk is unconfigured or the call fails; the caller keeps
    what it has rather than replacing a bad value with a worse one.
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_CLERK_API_BASE}/users/{clerk_user_id}",
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            )
        if resp.status_code >= 400:
            logger.warning("Clerk user fetch failed (%s)", resp.status_code)
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 — an admin page must not die on Clerk being slow
        logger.warning("Clerk user fetch errored for %s", clerk_user_id[:12])
        return None

    primary_id = data.get("primary_email_address_id")
    addresses = data.get("email_addresses") or []
    for entry in addresses:
        if entry.get("id") == primary_id and entry.get("email_address"):
            return entry["email_address"]
    for entry in addresses:
        if entry.get("email_address"):
            return entry["email_address"]
    return None
