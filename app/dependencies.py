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


async def get_current_user(
    claims: AuthClaims = Depends(authenticate),
) -> User:
    """Resolve the DB user from Clerk claims. 401 if not provisioned (unless
    DEMO_OPEN_ACCESS auto-attaches new users to the demo practice)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.clerk_user_id == claims.clerk_user_id)
        )
        user = result.scalar_one_or_none()
        if user is None and get_settings().demo_open_access:
            user = await _auto_provision_demo_user(session, claims.clerk_user_id)
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
    async with async_session_factory() as session:
        await set_tenant(session, practice.id)
        yield session
