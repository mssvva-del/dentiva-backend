"""Alembic must connect as a role that can change the schema.

The application role is deliberately not the owner of any table — that is what
makes RLS real, and it is also why it cannot ALTER anything. When the migration
URL is derived from it, the first schema change after the role switch fails with

    must be owner of table callback_requests

buried in an eighty-line Alembic traceback, at deploy time, in a log nobody is
watching. Production ran a build from 2 August for nine days for exactly this
reason: the PHI-encryption migration merged one day after the role switch and
could never apply.

There was already a comment in config.py describing this failure precisely. It
predicted the bug and prevented nothing, which is the argument for a test.
"""

from __future__ import annotations

import pytest

from app.config import Settings

_APP = "postgresql://dentiva_app:secret@db:5432/dentiva"
_OWNER = "postgresql://postgres:secret@db:5432/dentiva"


def _settings(**env) -> Settings:
    return Settings(_env_file=None, **env)


def test_migrations_prefer_the_owner_connection_over_the_app_role():
    """The exact production shape: an app role for traffic, an owner role for
    the admin pages, and nothing set for migrations."""
    s = _settings(database_url=_APP, database_url_platform=_OWNER)
    assert "postgres:" in s.database_url_sync
    assert "dentiva_app" not in s.database_url_sync


def test_an_explicit_sync_url_still_wins():
    """Someone deliberately pointing migrations somewhere must not be overridden
    by a fallback."""
    explicit = "postgresql://migrator:secret@db:5432/dentiva"
    s = _settings(
        database_url=_APP, database_url_platform=_OWNER, database_url_sync=explicit
    )
    assert "migrator" in s.database_url_sync


def test_a_single_url_deployment_still_works():
    """Local development and any deployment with one role: nothing to prefer,
    use what there is."""
    single = "postgresql://dentiva:secret@localhost:5432/dentiva"
    s = _settings(database_url=single)
    assert "dentiva:" in s.database_url_sync


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://", "postgresql+asyncpg://"])
def test_whatever_shape_the_platform_hands_us_becomes_a_psycopg2_url(scheme):
    """Railway hands out postgresql://, our own code rewrites to asyncpg, and
    Alembic needs psycopg2. The conversion has to survive all three."""
    s = _settings(
        database_url=_APP,
        database_url_platform=f"{scheme}postgres:secret@db:5432/dentiva",
    )
    assert s.database_url_sync.startswith("postgresql+psycopg2://")
