"""SQLAlchemy 2.0 async engine, session factory, and tenant helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_settings = get_settings()

engine = create_async_engine(_settings.database_url, echo=False, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an AsyncSession."""
    async with async_session_factory() as session:
        yield session


async def set_tenant(session: AsyncSession, practice_id: UUID) -> None:
    """Set the per-request tenant on the DB session for Row-Level Security.

    Postgres RLS policies read ``current_setting('app.current_practice_id')``.

    We use ``is_local => false`` (session/connection-scoped) rather than ``true``
    (transaction-scoped). Each request gets its own session bound to one
    connection, and statements may run in separate implicit transactions; a
    transaction-local setting would be lost between them. The setting is cleared
    when the connection is returned to the pool / closed.
    """
    await session.execute(
        text("SELECT set_config('app.current_practice_id', :pid, false)"),
        {"pid": str(practice_id)},
    )


def tenant_query(model, practice_id: UUID):  # noqa: ANN001, ANN201
    """Build a SELECT already filtered to the given practice (belt-and-suspenders)."""
    return select(model).where(model.practice_id == practice_id)
