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
from app.logging_config import practice_id_var


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_settings = get_settings()

engine = create_async_engine(_settings.database_url, echo=False, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

# ─────────────────────────────────────────────────────────────────────────────
# The PLATFORM connection — the deliberate hole in tenant isolation.
#
# Clinic traffic connects as an RLS-enforced role, so a query that forgets its
# tenant returns nothing instead of another clinic's PHI. But some of OUR OWN
# code legitimately reads across every clinic: the internal admin area (revenue,
# clinic list, system health) and the cross-clinic QA review of the shared agent.
# Under RLS those queries return empty, which is why production has been running
# on the superuser connection instead — trading the entire tenant boundary for a
# handful of internal screens.
#
# So the privilege gets its own connection rather than being handed to the whole
# app. DATABASE_URL_PLATFORM points at a role that may bypass RLS; every use of
# platform_session_factory is a place where cross-tenant reads are INTENDED, and
# is greppable in review. It falls back to the normal URL, so nothing changes
# until the two are actually pointed at different roles.
# ─────────────────────────────────────────────────────────────────────────────
_platform_url = _settings.database_url_platform or _settings.database_url
platform_engine = (
    engine
    if _platform_url == _settings.database_url
    else create_async_engine(_platform_url, echo=False, pool_pre_ping=True)
)
platform_session_factory = async_sessionmaker(platform_engine, expire_on_commit=False)


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
    # Mirror the bound tenant into the log context so every subsequent log line
    # for this request is tagged with the practice (non-PHI id only).
    practice_id_var.set(str(practice_id))
    await session.execute(
        text("SELECT set_config('app.current_practice_id', :pid, false)"),
        {"pid": str(practice_id)},
    )


def tenant_query(model, practice_id: UUID):  # noqa: ANN001, ANN201
    """Build a SELECT already filtered to the given practice (belt-and-suspenders)."""
    return select(model).where(model.practice_id == practice_id)
