"""Request-scoped dependencies: current user, current practice, tenant-bound DB."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import async_session_factory, set_tenant
from app.middleware.auth import AuthClaims, authenticate
from app.models.audit_log import AuditLog
from app.models.practice import Practice
from app.models.user import User

logger = logging.getLogger("dentiva.dependencies")


async def _auto_provision_demo_user(session, clerk_user_id: str) -> User | None:
    """Demo-only: attach an unknown Clerk user to the first (demo) practice.

    Lets you hand a login to a doctor/investor without per-user setup. Everyone
    shares the same demo data. Gated by DEMO_OPEN_ACCESS.
    """
    practice = (
        await session.execute(select(Practice).order_by(Practice.created_at).limit(1))
    ).scalar_one_or_none()
    if practice is None:
        return None
    user = User(
        id=uuid.uuid4(),
        clerk_user_id=clerk_user_id,
        practice_id=practice.id,
        email=f"{clerk_user_id}@demo.dentiva",
        role="staff",
    )
    session.add(user)
    try:
        await session.commit()
        await session.refresh(user)
    except IntegrityError:
        # Concurrent first request created it — re-read.
        await session.rollback()
        user = (
            await session.execute(
                select(User).where(User.clerk_user_id == clerk_user_id)
            )
        ).scalar_one_or_none()
    logger.info("demo open-access: provisioned user for clerk_id=%s", clerk_user_id[:12])
    return user


def _role_from_org_role(clerk_org_role: str | None) -> str:
    """Map a Clerk org role claim to our clinic role (admin-like → owner)."""
    if clerk_org_role and clerk_org_role.split(":")[-1].lower() in {"admin", "owner"}:
        return "owner"
    return "staff"


async def _lazy_provision_user(session, claims: AuthClaims) -> User | None:
    """Provision a user (and their practice if needed) from a VERIFIED JWT.

    In-spec "first login without a clinic → onboarding" path, and a safety net for
    delayed/failed Clerk webhooks. Fully multi-tenant: keys off the caller's OWN
    org id (never the demo practice).

    Solo signup (no Clerk org — a single doctor who just signed up with an email):
    give them their OWN fresh practice keyed on their user id, so they land in the
    onboarding wizard instead of a broken empty dashboard. A real Clerk org can be
    linked to this practice later without data loss.
    """
    org_id = claims.clerk_org_id or f"solo_{claims.clerk_user_id}"

    practice = (
        await session.execute(
            select(Practice).where(Practice.clerk_org_id == org_id)
        )
    ).scalar_one_or_none()

    practice_was_new = False
    if practice is None:
        # The org creator's first login: stand up their practice in onboarding and
        # make them owner. Mirrors the organization.created + membership webhooks.
        practice_was_new = True
        practice = Practice(
            id=uuid.uuid4(),
            clerk_org_id=org_id,
            name="New practice",
            pms_system="none",
            business_hours={d: None for d in
                            ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
            languages_enabled=["en", "es"],
            status="onboarding",
            onboarding_step=1,
        )
        session.add(practice)
        await session.flush()
        role = "owner"
    else:
        # Joining an existing practice: owner if no owner yet (they created it),
        # else use the JWT org role (admin→owner, else staff).
        has_owner = (
            await session.execute(
                select(User).where(
                    User.practice_id == practice.id,
                    User.role == "owner",
                    User.status == "active",
                )
            )
        ).scalar_one_or_none() is not None
        role = "owner" if not has_owner else _role_from_org_role(claims.clerk_org_role)

    user = User(
        id=uuid.uuid4(),
        clerk_user_id=claims.clerk_user_id,
        practice_id=practice.id,
        email=claims.email or f"{claims.clerk_user_id}@clerk.local",
        role=role,
        is_internal=False,
        status="active",
    )
    session.add(user)
    try:
        await session.commit()
        await session.refresh(user)
    except IntegrityError:
        # Concurrent first request created it — re-read.
        await session.rollback()
        user = (
            await session.execute(
                select(User).where(User.clerk_user_id == claims.clerk_user_id)
            )
        ).scalar_one_or_none()
    logger.info(
        "lazy provisioning: user=%s practice_org=%s role=%s",
        claims.clerk_user_id[:12], org_id[:12], role,
    )
    # S6: audit the lazy-provisioning path — a high-privilege, irreversible
    # auto-create of a practice+user from a JWT claim. Non-fatal: the user is
    # already committed, so a failed audit write must never block login.
    if user is not None:
        try:
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    practice_id=practice.id,
                    user_id=user.id,
                    action="lazy_provisioning",
                    resource_type="user",
                    resource_id=user.id,
                    audit_metadata={
                        "clerk_org_id": org_id,
                        "role": role,
                        "practice_created": practice_was_new,
                    },
                )
            )
            await session.commit()
        except Exception as audit_exc:
            logger.warning("lazy provisioning audit log failed: %s", audit_exc)
            await session.rollback()
    return user


async def get_current_user(
    claims: AuthClaims = Depends(authenticate),
) -> User:
    """Resolve the DB user from Clerk claims. 401 if not provisioned.

    Two optional auto-provision paths (both off by default): DEMO_OPEN_ACCESS
    (attach to the demo practice — single-tenant demo) and LAZY_PROVISIONING
    (provision from the caller's own verified org — multi-tenant, in-spec
    first-login onboarding). They are mutually exclusive in practice; demo takes
    precedence if both are somehow on.
    """
    settings = get_settings()
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.clerk_user_id == claims.clerk_user_id)
        )
        user = result.scalar_one_or_none()
        if user is None and settings.demo_open_access:
            user = await _auto_provision_demo_user(session, claims.clerk_user_id)
        elif user is None and settings.lazy_provisioning:
            user = await _lazy_provision_user(session, claims)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not provisioned for this Dentiva instance.",
        )
    return user


async def get_current_practice(
    user: User = Depends(get_current_user),
) -> Practice:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Practice).where(Practice.id == user.practice_id)
        )
        practice = result.scalar_one_or_none()
    if practice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Practice not found."
        )
    return practice


async def get_tenant_db(
    practice: Practice = Depends(get_current_practice),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a DB session with RLS tenant context set to the caller's practice."""
    # WHY separate from a plain get_db: RLS policies key off app.current_practice_id,
    # so PHI routes MUST take this dependency — a bare session has no tenant set and
    # (as dentiva_app) would see zero rows, or as superuser would see ALL tenants.
    async with async_session_factory() as session:
        # WHY before yield: set_tenant runs inside the same transaction the route
        # will query on. If a query ran before this, RLS would evaluate with an
        # unset practice id → wrong/empty results. Tenant must be set first.
        await set_tenant(session, practice.id)
        yield session
