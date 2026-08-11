"""SQLAlchemy 2.0 async engine, session factory, and tenant helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

from sqlalchemy import event, select, text
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


def _clear_tenant_on_checkout(dbapi_connection, connection_record, connection_proxy):
    """Hand out a connection with no tenant bound to it. Ever.

    set_tenant sets the GUC at SESSION scope, deliberately, because a handler
    commits partway through and transaction scope would lose the binding. The
    cost of that choice is that the value outlives the request: PostgreSQL undoes
    a SET when its transaction rolls back, and the pool's default reset IS a
    rollback — so the binding vanishes on the paths that roll back and SURVIVES
    on the paths that commit, which is every handler we have.

    Then the connection goes back in the pool carrying the last clinic it served.
    Any query that reaches an RLS table without calling set_tenant first — a new
    code path, an early return, a helper called before the tenant is resolved —
    does not fail closed. It reads as whoever held that connection last.

    The test suite cannot see this. conftest uses NullPool, so every test gets a
    fresh connection where a forgotten set_tenant leaves the GUC empty, RLS
    matches nothing, and the omission looks harmless. Production pools. The two
    environments differ exactly on the mechanism that decides whether the bug is
    invisible or catastrophic.

    Clearing on CHECKOUT rather than on return is not a stylistic choice: the
    "reset" event's own SQL is undone by the rollback that follows it. Verified
    both ways against a real pooled engine before this was written.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT set_config('app.current_practice_id', '', false)")
    finally:
        cursor.close()


event.listens_for(engine.sync_engine, "checkout")(_clear_tenant_on_checkout)

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

# The platform role may bypass RLS, so a stale GUC on its connections changes
# nothing about what it can read. It gets the same hook anyway: the two engines
# are the SAME object whenever the URLs match, and a rule with an exception is a
# rule someone removes the wrong half of later.
if platform_engine is not engine:
    event.listens_for(platform_engine.sync_engine, "checkout")(_clear_tenant_on_checkout)


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
