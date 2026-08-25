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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_tenant
from app.models.dentiva_staff import DentivaStaff
from app.models.invitation import Invitation
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


def _invited_dentiva_role(data: dict) -> str | None:
    """Dentiva admin role stamped by an ADM9 staff invitation, if any.

    An instance-level Clerk invitation carries public_metadata that Clerk copies
    onto the user at signup — {"dentiva_role": "support"} marks the signup as an
    internal-staff invite. Validated against the RBAC map: an unknown/garbage
    value must NOT create privileged rows.
    """
    from app.auth.permissions import ADMIN_ROLE_PERMISSIONS

    role = (data.get("public_metadata") or {}).get("dentiva_role")
    return role if role in ADMIN_ROLE_PERMISSIONS else None


async def handle_user_created(session: AsyncSession, data: dict) -> str:
    """Upsert a users row for a freshly signed-up Clerk user.

    No org yet → practice_id NULL, least-privilege role. The membership event
    later attaches them to a practice with the real role.

    ADM9: signups that arrive via an internal-staff invitation (dentiva_role in
    public_metadata, stamped by POST /api/admin/staff/invite) are provisioned as
    is_internal=True + a dentiva_staff row — no manual script step.
    """
    clerk_user_id = data.get("id")
    if not clerk_user_id:
        raise ValueError("user.created missing id")
    email = _primary_email(data)
    staff_role = _invited_dentiva_role(data)

    existing = (
        await session.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent: keep role/practice as-is, just refresh email if we learned it.
        if email and existing.email != email:
            existing.email = email
        logger.info("clerk user.created: user already present (%s)", clerk_user_id[:12])
        return "noop"

    user = User(
        id=uuid.uuid4(),
        clerk_user_id=clerk_user_id,
        practice_id=None,          # no clinic until a membership links one
        email=email or f"{clerk_user_id}@unknown.clerk",
        role="viewer" if staff_role is None else "staff",
        is_internal=staff_role is not None,
        status="active",
    )
    session.add(user)
    if staff_role is not None:
        await session.flush()
        session.add(DentivaStaff(id=uuid.uuid4(), user_id=user.id, role=staff_role))
        logger.info(
            "clerk user.created: provisioned INTERNAL staff %s as %s",
            clerk_user_id[:12], staff_role,
        )
        return f"created_internal:{staff_role}"
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

    # Prefer an explicitly invited role over the Clerk-role mapping: if this user
    # was invited (pending invitation matching their email in THIS practice), use
    # the role the owner chose and mark the invitation accepted. RLS guards the
    # invitations table, so bind the tenant context first.
    email = (pud.get("identifier") or "").strip().lower()
    role = map_clerk_role(clerk_role)
    invitation: Invitation | None = None
    if email:
        await set_tenant(session, practice.id)
        invitation = (
            await session.execute(
                select(Invitation).where(
                    Invitation.practice_id == practice.id,
                    func.lower(Invitation.email) == email,
                    Invitation.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if invitation is not None:
            role = invitation.role
            invitation.status = "accepted"
            logger.info("membership.created: matched invitation, role=%s", role)

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
PLACEHOLDER_EMAIL_SUFFIX = "@unknown.clerk"

# Two of them, written by two different paths, and healing only one is how the
# first real clinic kept showing a machine id after the fix shipped: the webhook
# writes "@unknown.clerk", while a user auto-created on their first authenticated
# request (a JWT with no email claim) gets "@clerk.local". Anything that repairs
# a placeholder has to know about both.
PLACEHOLDER_EMAIL_SUFFIXES = ("@unknown.clerk", "@clerk.local")


def is_placeholder_email(email: str | None) -> bool:
    """True for an address we invented to satisfy NOT NULL, not one a person has."""
    return bool(email) and email.endswith(PLACEHOLDER_EMAIL_SUFFIXES)


async def handle_user_updated(session, data: dict) -> str:
    """Learn the real email address when Clerk finally has one.

    user.created can arrive before the address is attached and verified — and
    when it does, we store ``<clerk_id>@unknown.clerk`` so the NOT NULL column
    has something. Clerk then sends user.updated with the real address, which we
    ignored, so the placeholder became permanent: the admin screen showed a
    machine id where the practice owner's email belongs, and nobody could
    contact the clinic from the record that exists to describe it.

    Deliberately narrow: this refreshes the email and nothing else. Role,
    practice and internal-staff status are decided by membership events, and a
    profile edit must never be able to move a user between clinics.
    """
    clerk_user_id = data.get("id")
    if not clerk_user_id:
        return "noop"
    email = _primary_email(data)
    if not email:
        return "noop"
    user = (
        await session.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    ).scalar_one_or_none()
    if user is None or user.email == email:
        return "noop"
    # Never overwrite a real address with a placeholder — the guard above rules
    # out an empty one, and this rules out a regression from a partial payload.
    if is_placeholder_email(email):
        return "noop"
    user.email = email
    logger.info("clerk user.updated: learned email for %s", clerk_user_id[:12])
    return "email_updated"


EVENT_HANDLERS = {
    "user.created": handle_user_created,
    "user.updated": handle_user_updated,
    "organization.created": handle_organization_created,
    "organizationMembership.created": handle_membership_created,
    "user.deleted": handle_user_deleted,
}
