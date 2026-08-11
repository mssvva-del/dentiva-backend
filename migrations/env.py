"""Alembic environment. Runs synchronously via psycopg2 (DATABASE_URL_SYNC)."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import models so they register on Base.metadata for autogenerate.
import app.models  # noqa: F401,E402
from app.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url_sync)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _refuse_to_run_without_ddl_rights(connection) -> None:
    """Stop with a readable sentence rather than a traceback eighty lines long.

    The application role is deliberately not the owner of any table — that is
    what makes RLS real — so it cannot ALTER anything. Running migrations as it
    fails on the first schema change with "must be owner of table X", buried in
    an Alembic stack trace, at deploy time, in a log nobody is watching. That
    cost production nine days on a stale build.
    """
    from sqlalchemy import text

    row = connection.execute(text("""
        SELECT current_user,
               (SELECT count(*) FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND pg_get_userbyid(c.relowner) = current_user) AS owned
    """)).first()
    if row is None:
        return
    user, owned = row
    if owned:
        return
    # No tables at all is a fresh database — the first migration creates them and
    # the role that runs it becomes the owner. That is fine.
    total = connection.execute(text(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
    )).scalar_one()
    if not total:
        return
    raise SystemExit(
        f"\nMigrations are connected as '{user}', which owns none of the "
        f"{total} tables in this database, so every ALTER will fail.\n\n"
        "Alembic takes DATABASE_URL_SYNC, then DATABASE_URL_PLATFORM, then "
        "DATABASE_URL. Point one of the first two at the owner role.\n"
    )


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _refuse_to_run_without_ddl_rights(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
