"""Request-scoped dependencies: current user, current practice, tenant-bound DB."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory, set_tenant
from app.middleware.auth import AuthClaims, authenticate
from app.models.practice import Practice
from app.models.user import User


async def get_current_user(
    claims: AuthClaims = Depends(authenticate),
) -> User:
    """Resolve the DB user from Clerk claims. 401 if not provisioned."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.clerk_user_id == claims.clerk_user_id)
        )
        user = result.scalar_one_or_none()
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
