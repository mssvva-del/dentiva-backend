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
