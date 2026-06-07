"""Clerk webhook → database provisioning (Platform Iter 1, Phase B1).

Translates Clerk events into our own tables. Clerk owns identity (users, orgs,
memberships); we mirror the parts we need and own the *authorization* (role,
is_internal, practice lifecycle) in our DB — never trusting Clerk for perms.

Event → effect:
  user.created                    → upsert users row (no clinic yet, least-priv)
  organization.created            → create practices row in 'onboarding'
  organizationMembership.created  → link user ↔ practice + assign clinic role
  user.deleted                    → SOFT-disable the user (never hard delete)

Every handler is IDEMPOTENT (Clerk retries webhooks): upserts key on the Clerk
id, so a redelivered event is a no-op. users/practices are NOT RLS-protected, so
a plain (non-tenant-bound) session writes them — which is required here because
organization.created has no tenant context yet.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.practice import Practice
from app.models.user import User

logger = logging.getLogger("dentiva.webhooks.clerk")

# Default business-hours shape (all days closed). The onboarding wizard fills the
# real hours; this just satisfies the NOT NULL JSONB column at creation time.
_DEFAULT_BUSINESS_HOURS: dict[str, None] = {
    "mon": None, "tue": None, "wed": None, "thu": None,
    "fri": None, "sat": None, "sun": None,
}

# Iter 1 supports English + Spanish only (Russian removed). New practices start
# bilingual; the wizard lets them narrow it.
_DEFAULT_LANGUAGES = ["en", "es"]


def map_clerk_role(clerk_role: str | None) -> str:
    """Map a Clerk organization role to our clinic role.

    Clerk's defaults are 'org:admin' / 'org:member' (newer) or 'admin' /
    'basic_member' (older). Anything admin-like → owner; everything else →
    staff (a safe middle: can work the desk, can't manage billing/team). The
    invitation flow (B3) overrides this with the explicitly invited role.
    """
    if not clerk_role:
        return "staff"
    normalized = clerk_role.split(":")[-1].lower()
    if normalized in {"admin", "owner"}:
        return "owner"
    return "staff"


def _primary_email(data: dict) -> str:
    """Extract the primary email from a Clerk user payload (best effort)."""
    primary_id = data.get("primary_email_address_id")
    emails = data.get("email_addresses") or []
    for e in emails:
        if e.get("id") == primary_id:
            return e.get("email_address", "")
    if emails:
        return emails[0].get("email_address", "")
    return ""


async def handle_user_created(session: AsyncSession, data: dict) -> str:
    """Upsert a users row for a freshly signed-up Clerk user.

    No org yet → practice_id NULL, least-privilege role. The membership event
    later attaches them to a practice with the real role.
    """
    clerk_user_id = data.get("id")
    if not clerk_user_id:
        raise ValueError("user.created missing id")
    email = _primary_email(data)

    existing = (
        await session.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent: keep role/practice as-is, just refresh email if we learned it.
        if email and existing.email != email:
            existing.email = email
        logger.info("clerk user.created: user already present (%s)", clerk_user_id[:12])
        return "noop"

    session.add(
        User(
            id=uuid.uuid4(),
            clerk_user_id=clerk_user_id,
            practice_id=None,          # no clinic until a membership links one
            email=email or f"{clerk_user_id}@unknown.clerk",
            role="viewer",             # least privilege until membership assigns
            is_internal=False,
            status="active",
        )
    )
    logger.info("clerk user.created: provisioned user %s", clerk_user_id[:12])
    return "created"


async def handle_organization_created(session: AsyncSession, data: dict) -> str:
    """Create a practices row for a new Clerk organization (clinic), in onboarding."""
    clerk_org_id = data.get("id")
    if not clerk_org_id:
        raise ValueError("organization.created missing id")
    name = data.get("name") or "New practice"

    existing = (
        await session.execute(
            select(Practice).where(Practice.clerk_org_id == clerk_org_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("clerk organization.created: practice exists (%s)", clerk_org_id[:12])
        return "noop"

    session.add(
        Practice(
            id=uuid.uuid4(),
            clerk_org_id=clerk_org_id,
            name=name,
            pms_system="none",                 # set during onboarding
            business_hours=_DEFAULT_BUSINESS_HOURS,
            languages_enabled=list(_DEFAULT_LANGUAGES),
            status="onboarding",
            onboarding_step=1,                 # wizard starts at step 1
        )
    )
    logger.info("clerk organization.created: provisioned practice %s", clerk_org_id[:12])
    return "created"


async def handle_membership_created(session: AsyncSession, data: dict) -> str:
    """Link a user to a practice and assign their clinic role.

    Handles either event ordering: if the user row doesn't exist yet (membership
    arrived before user.created), we create a minimal one so the link holds.
    """
    org = data.get("organization") or {}
    clerk_org_id = org.get("id")
    pud = data.get("public_user_data") or {}
    clerk_user_id = pud.get("user_id") or pud.get("identifier")
    clerk_role = data.get("role")
    if not (clerk_org_id and clerk_user_id):
        raise ValueError("organizationMembership.created missing org or user id")

    practice = (
        await session.execute(
            select(Practice).where(Practice.clerk_org_id == clerk_org_id)
        )
    ).scalar_one_or_none()
    if practice is None:
        # Membership before organization.created — rare but possible. Skip and let
        # Clerk redelivery (or organization.created) reconcile; do NOT guess a
        # practice. Logged so it's visible.
        logger.warning(
            "membership.created: practice not found for org %s — deferring",
            clerk_org_id[:12],
        )
        return "deferred"

    role = map_clerk_role(clerk_role)
    user = (
        await session.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    ).scalar_one_or_none()
    if user is None:
        session.add(
            User(
                id=uuid.uuid4(),
                clerk_user_id=clerk_user_id,
                practice_id=practice.id,
                email=pud.get("identifier", "") or f"{clerk_user_id}@unknown.clerk",
                role=role,
                is_internal=False,
                status="active",
            )
        )
        logger.info(
            "membership.created: created+linked user %s to practice (role=%s)",
            clerk_user_id[:12], role,
        )
        return "created_and_linked"

    # Don't clobber an internal staff user's world with a clinic membership.
    if user.is_internal:
        logger.warning(
            "membership.created: ignoring clinic membership for internal user %s",
            clerk_user_id[:12],
        )
        return "noop_internal"

    user.practice_id = practice.id
    user.role = role
    user.status = "active"
    logger.info(
        "membership.created: linked user %s to practice (role=%s)",
        clerk_user_id[:12], role,
    )
    return "linked"


async def handle_user_deleted(session: AsyncSession, data: dict) -> str:
    """Soft-disable a deleted Clerk user. We NEVER hard-delete (audit + safety).

    The row stays so historical references (audit logs, call ownership) remain
    intact; the user simply can't authenticate (status='disabled', and Clerk no
    longer issues them a token anyway).
    """
    clerk_user_id = data.get("id")
    if not clerk_user_id:
        raise ValueError("user.deleted missing id")
    user = (
        await session.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    ).scalar_one_or_none()
    if user is None:
        logger.info("user.deleted: no local user for %s", clerk_user_id[:12])
        return "noop"
    user.status = "disabled"
    logger.info("user.deleted: soft-disabled user %s", clerk_user_id[:12])
    return "disabled"


# Dispatch table: Clerk event type → handler.
EVENT_HANDLERS = {
    "user.created": handle_user_created,
    "organization.created": handle_organization_created,
    "organizationMembership.created": handle_membership_created,
    "user.deleted": handle_user_deleted,
}
