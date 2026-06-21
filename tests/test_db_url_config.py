"""DATABASE_URL / DATABASE_URL_SYNC normalization (RLS cutover prerequisite).

The app runtime uses DATABASE_URL (async/asyncpg); Alembic uses DATABASE_URL_SYNC
(sync/psycopg2). For the RLS cutover the app must connect as a restricted role
while migrations keep running as the superuser — so an explicitly-set
DATABASE_URL_SYNC must be honored, not silently re-derived from DATABASE_URL.
"""

from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _clear_db_env(monkeypatch):
    # Isolate from the developer's shell/.env so the test controls both URLs.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_SYNC", raising=False)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_sync_derived_from_async_when_not_explicit():
    s = _settings(database_url="postgresql://u:p@h:5432/db")
    assert s.database_url == "postgresql+asyncpg://u:p@h:5432/db"
    assert s.database_url_sync == "postgresql+psycopg2://u:p@h:5432/db"


def test_explicit_sync_url_is_honored():
    # Cutover shape: app → restricted role, Alembic → superuser.
    s = _settings(
        database_url="postgresql://dentiva_app:pw@h:5432/db",
        database_url_sync="postgresql://dentiva:superpw@h:5432/db",
    )
    assert s.database_url == "postgresql+asyncpg://dentiva_app:pw@h:5432/db"
    # Sync stays on the superuser creds, normalized to psycopg2.
    assert s.database_url_sync == "postgresql+psycopg2://dentiva:superpw@h:5432/db"
    # The two roles are genuinely different (the whole point of the cutover).
    assert "dentiva_app" not in s.database_url_sync


def test_explicit_sync_already_psycopg2_passthrough():
    s = _settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        database_url_sync="postgresql+psycopg2://dentiva:superpw@h:5432/db",
    )
    assert s.database_url_sync == "postgresql+psycopg2://dentiva:superpw@h:5432/db"


def test_postgres_scheme_normalized_for_both():
    s = _settings(database_url="postgres://u:p@h:5432/db")
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.database_url_sync.startswith("postgresql+psycopg2://")
