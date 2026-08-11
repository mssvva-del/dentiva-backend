"""A pooled connection must never carry one clinic's tenant binding to the next.

This is the one test in the suite that builds its own engine, and it has to.
conftest uses NullPool — every test gets a fresh physical connection — which is
correct for test isolation and is exactly why this class of bug is invisible
here. On a fresh connection a forgotten set_tenant leaves the GUC empty, RLS
matches nothing, and the omission looks harmless. Production pools, and there the
same omission reads as whoever held that connection last.

The two environments differ precisely on the mechanism that decides whether a
missing set_tenant is a no-op or a cross-tenant PHI leak. So this test opts out
of the harness and uses a real pool.

Why the binding survives at all: set_tenant sets the GUC at session scope on
purpose, because handlers commit partway through and transaction scope would drop
it mid-request. PostgreSQL undoes a SET when its transaction rolls back, and the
pool's default reset IS a rollback — so the value disappears on paths that roll
back and persists on paths that commit, which is every handler we have.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import _clear_tenant_on_checkout

_READ = text("SELECT current_setting('app.current_practice_id', true)")
_BIND = text("SELECT set_config('app.current_practice_id', :p, false)")


def _pooled(*, with_hook: bool):
    """One physical connection, reused — the production shape, concentrated."""
    from sqlalchemy import event

    engine = create_async_engine(
        get_settings().database_url, pool_size=1, max_overflow=0
    )
    if with_hook:
        event.listens_for(engine.sync_engine, "checkout")(_clear_tenant_on_checkout)
    return engine


@pytest.mark.parametrize("with_hook,expected_leak", [(False, True), (True, False)])
async def test_a_committed_tenant_binding_does_not_reach_the_next_request(
    with_hook, expected_leak
):
    """Bind a tenant, commit as every handler does, hand the connection back.

    The next caller has not bound anything. Without the hook it sees the previous
    clinic; with it, nothing. Both halves are asserted so the test cannot pass by
    the leak simply not existing on some future driver — if the unguarded case
    ever stops leaking, this goes red and someone re-reads the assumption.
    """
    engine = _pooled(with_hook=with_hook)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    practice_id = str(uuid.uuid4())
    try:
        async with session_factory() as session:
            await session.execute(_BIND, {"p": practice_id})
            await session.commit()

        async with session_factory() as session:
            seen = (await session.execute(_READ)).scalar()

        assert (seen == practice_id) is expected_leak, (
            f"with_hook={with_hook}: connection came out of the pool bound to "
            f"{seen!r}"
        )
    finally:
        await engine.dispose()


async def test_a_rolled_back_binding_never_leaked_and_still_does_not():
    """The half that always looked fine, pinned so nobody 'simplifies' the hook
    away after testing only this path. PostgreSQL rolls a SET back with its
    transaction, so this case was never the problem — which is what made the
    other one so easy to miss."""
    engine = _pooled(with_hook=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    practice_id = str(uuid.uuid4())
    try:
        async with session_factory() as session:
            await session.execute(_BIND, {"p": practice_id})
            # no commit — the session closes and SQLAlchemy rolls back
        async with session_factory() as session:
            assert (await session.execute(_READ)).scalar() != practice_id
    finally:
        await engine.dispose()
