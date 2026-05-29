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
        await conn.execute(text("ALTER TABLE patients ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE patients FORCE ROW LEVEL SECURITY"))
        await conn.execute(
            text(
                "CREATE POLICY tenant_isolation ON patients USING "
                "(practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid)"
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
    await owner_engine.dispose()

    # App engine connects as the RLS-enforced dentiva_app role.
    # NullPool avoids a single asyncpg connection being shared across concurrent
    # tasks (the ASGI request task vs. the test task).
    test_engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)

    # Repoint the app's engine/session factory at the test DB.
    app_db.engine = test_engine
    app_db.async_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    deps.async_session_factory = app_db.async_session_factory

    yield

    await test_engine.dispose()


@pytest_asyncio.fixture
async def db_session(_prepare_database) -> AsyncGenerator[AsyncSession, None]:
    async with app_db.async_session_factory() as session:
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
