"""Pytest fixtures. Tests run against the local Postgres (Docker) on a
dedicated ``dentiva_test`` database so they never touch dev data.

Requires: ``docker compose up -d db`` and a valid ENCRYPTION_KEY in .env.
"""

from __future__ import annotations

# Force dev-bypass auth + point app at the test DB BEFORE importing app modules.
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ["AUTH_DEV_BYPASS"] = "true"
# No outbound SMS from the test suite, ever. A developer .env with real Twilio
# credentials was being picked up here, so every booking test fired live requests
# at Twilio — real money, real rate limits, and a suite that hung on the network
# instead of failing. Tests that care about SMS assert on the call, not the send.
os.environ["SMS_ENABLED"] = "false"
os.environ["TWILIO_ACCOUNT_SID"] = ""
os.environ["TWILIO_AUTH_TOKEN"] = ""
# Mount the Groq custom-LLM relay so its WS tests run (prod keeps it off by default).
os.environ["ENABLE_LLM_RELAY"] = "true"
# App connects as the RLS-enforced dentiva_app role; schema mgmt uses the owner.
TEST_DB_URL = "postgresql+asyncpg://dentiva_app:dentiva_app@localhost:5432/dentiva_test"
OWNER_TEST_DB_URL = "postgresql+asyncpg://dentiva:dentiva@localhost:5432/dentiva_test"
os.environ["DATABASE_URL"] = TEST_DB_URL

import app.db as app_db  # noqa: E402
import app.dependencies as deps  # noqa: E402
import app.models  # noqa: F401,E402  (register models on Base.metadata)
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_URL = "postgresql+asyncpg://dentiva:dentiva@localhost:5432/dentiva"


@pytest_asyncio.fixture
async def _prepare_database() -> AsyncGenerator[None, None]:
    # Create the test database if missing.
    admin_engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'dentiva_test'")
            )
        ).scalar()
        if not exists:
            await conn.execute(text("CREATE DATABASE dentiva_test"))
    await admin_engine.dispose()

    # Rebuild a fresh schema as the OWNER (drop/create + RLS policy + grants).
    # This mirrors the migrations for tests.
    owner_engine = create_async_engine(OWNER_TEST_DB_URL, poolclass=NullPool)
    async with owner_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # Apply the SAME row-level security the migrations apply in prod, for
        # EVERY RLS-protected table — so tenant isolation is actually EXERCISED by
        # tests (previously only `patients` had RLS here, so the RLS on
        # calls/bookings/callbacks/waitlist/invitations went untested).
        for _t in (
            "patients", "calls", "bookings", "callback_requests",
            "waitlist_entries", "invitations",
            "reactivation_campaigns", "reactivation_targets", "reactivation_touches",
        ):
            await conn.execute(text(f"ALTER TABLE {_t} ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text(f"ALTER TABLE {_t} FORCE ROW LEVEL SECURITY"))
            await conn.execute(
                text(
                    f"CREATE POLICY tenant_isolation ON {_t} USING (practice_id = "
                    "NULLIF(current_setting('app.current_practice_id', true), '')::uuid)"
                )
            )
        # Grant the app role DML on the freshly (re)created tables.
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                "TO dentiva_app"
            )
        )
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO dentiva_app"))
        # Trusted tenant-routing function (mirrors the migration). SECURITY
        # DEFINER + owned by the superuser → bypasses RLS to resolve only the
        # practice_id for a retell_call_id (the webhook needs this before it can
        # bind the tenant). Created here because tests build schema via create_all.
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION dentiva_practice_for_retell_call(p_call_id text) "
                "RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS "
                "$$ SELECT practice_id FROM calls WHERE retell_call_id = p_call_id LIMIT 1; $$;"
            )
        )
        await conn.execute(
            text(
                "GRANT EXECUTE ON FUNCTION dentiva_practice_for_retell_call(text) "
                "TO dentiva_app"
            )
        )
    await owner_engine.dispose()

    # App engine connects as the RLS-enforced dentiva_app role.
    # NullPool avoids a single asyncpg connection being shared across concurrent
    # tasks (the ASGI request task vs. the test task).
    test_engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)

    _make_an_unbound_tenant_query_loud(test_engine)

    # Repoint the app's engine/session factory at the test DB.
    app_db.engine = test_engine
    app_db.async_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    deps.async_session_factory = app_db.async_session_factory
    # The platform connection (internal cross-clinic reads: admin area, QA review)
    # is a SEPARATE factory in the app. Point it at the OWNER role, mirroring
    # production's intent — the app role is RLS-enforced, and only this connection
    # may read across tenants. Leaving it unbound would also hand the tests an
    # engine attached to a different event loop.
    app_db.platform_engine = create_async_engine(OWNER_TEST_DB_URL, poolclass=NullPool)
    app_db.platform_session_factory = async_sessionmaker(
        app_db.platform_engine, expire_on_commit=False
    )

    # SEPARATE seeding session factory bound to the OWNER role (superuser →
    # bypasses RLS). The `db_session` fixture is a test harness for setup +
    # inspection, so it gets DBA-style access to insert/read any tenant's rows
    # without threading set_tenant everywhere. The APP path (client →
    # async_session_factory as dentiva_app) stays RLS-enforced, so tenant
    # isolation is still genuinely tested through the real API surface.
    global _seed_engine, _seed_session_factory
    _seed_engine = create_async_engine(OWNER_TEST_DB_URL, poolclass=NullPool)
    _seed_session_factory = async_sessionmaker(_seed_engine, expire_on_commit=False)

    yield

    await app_db.platform_engine.dispose()
    await test_engine.dispose()
    await _seed_engine.dispose()


# Set per-test in _prepare_database (owner/superuser → bypasses RLS for seeding).
_seed_engine = None
_seed_session_factory = None


# Tables whose rows belong to exactly one clinic. Mirrors REQUIRED_RLS in
# scripts/check_rls_coverage.py, which the CI gate enforces at HEAD.
_TENANT_TABLES = (
    "patients", "calls", "bookings", "callback_requests", "waitlist_entries",
    "invitations", "reactivation_campaigns", "reactivation_targets",
    "reactivation_touches",
)


class UnboundTenantQuery(AssertionError):
    """A tenant table was read with no clinic bound to the connection."""


def _make_an_unbound_tenant_query_loud(engine) -> None:
    """Turn "RLS returned nothing" into an error, in tests only.

    RLS fails closed, which is the right design and an awful diagnostic: a query
    that forgets set_tenant does not raise, it returns an empty result. The
    handler then reads that as "this patient has no appointment" or "there is no
    such call" and answers the caller accordingly.

    That is not hypothetical. join_waitlist read the calls row before binding a
    tenant, found nothing every single time, and fell through to guessing the
    practice — which happens to be correct while exactly one clinic exists, so
    the tests were green and the feature was broken for the second clinic.

    Production keeps the silent behaviour on purpose: an exception mid-call is
    worse for a patient than a degraded answer. Tests do not have patients, so
    here the same mistake is a failure with the query attached.
    """
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if not lowered.lstrip().startswith("select"):
            return
        if not any(f" {table}" in lowered for table in _TENANT_TABLES):
            return
        # Routing legitimately runs before a tenant is known, through a
        # SECURITY DEFINER function that returns a practice_id and no PHI.
        if "dentiva_practice_for_retell_call" in lowered:
            return
        bound = conn.exec_driver_sql(
            "SELECT current_setting('app.current_practice_id', true)"
        ).scalar()
        if bound:
            return
        raise UnboundTenantQuery(
            "a tenant table was queried with no clinic bound to the connection. "
            "RLS will return zero rows and the caller will read that as 'not "
            "found', which is how join_waitlist was broken for a year of "
            "single-clinic testing.\n\n"
            f"{statement.strip()[:400]}"
        )


@pytest_asyncio.fixture
async def db_session(_prepare_database) -> AsyncGenerator[AsyncSession, None]:
    # Privileged seeding/inspection session (bypasses RLS). NOT the app path.
    async with _seed_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(_prepare_database) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()


# ---- Seeding helpers -------------------------------------------------------

DEFAULT_HOURS = {
    "mon": {"open": "09:00", "close": "18:00"},
    "tue": {"open": "09:00", "close": "18:00"},
    "wed": {"open": "09:00", "close": "18:00"},
    "thu": {"open": "09:00", "close": "18:00"},
    "fri": {"open": "09:00", "close": "17:00"},
    "sat": None,
    "sun": None,
}


async def seed_practice(
    session: AsyncSession, *, name: str, clerk_org_id: str, clerk_user_id: str
):
    from app.models.practice import Practice
    from app.models.user import User

    practice = Practice(
        id=uuid.uuid4(),
        clerk_org_id=clerk_org_id,
        name=name,
        timezone="America/New_York",
        phone_number="+15559876543",
        pms_system="open_dental",
        languages_enabled=["en"],
        business_hours=DEFAULT_HOURS,
    )
    session.add(practice)
    await session.flush()
    user = User(
        id=uuid.uuid4(),
        clerk_user_id=clerk_user_id,
        practice_id=practice.id,
        email=f"{clerk_user_id}@example.com",
        role="owner",
    )
    session.add(user)
    await session.commit()
    return practice, user
