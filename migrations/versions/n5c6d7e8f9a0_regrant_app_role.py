"""re-grant dentiva_app on everything that exists today

The app is supposed to connect as ``dentiva_app`` so Row-Level Security actually
applies (a superuser is exempt from every policy). Production has been on the
superuser connection, so the moment DATABASE_URL is repointed, any table created
by a migration after ``b2c3d4e5f6a7_app_role`` — thirteen migrations create tables
— would have to have picked up its privileges from ALTER DEFAULT PRIVILEGES. That
only applies to objects created by the same role that ran the ALTER, so a single
migration run under a different role leaves a table the app cannot read, and the
switch fails at runtime with "permission denied for table …" on a live call.

This re-grants across the whole current schema, idempotently, so the switch turns
into a config change with nothing left to discover. Sequences are included even
though the schema is UUID-keyed today: a future SERIAL/BIGSERIAL column would
otherwise fail on insert only, and only in production.

The role's password is deliberately NOT touched here. b2c3d4e5f6a7 created it
with the literal password 'dentiva_app', which is in git; it must be rotated by
hand before the role is used for anything real.

Revision ID: n5c6d7e8f9a0
Revises: m4b5c6d7e8f9
"""

from __future__ import annotations

from alembic import op

revision = "n5c6d7e8f9a0"
down_revision = "m4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op when the role doesn't exist (a fresh local DB that never ran the
    # role migration) — GRANT to a missing role is an error, not a warning.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dentiva_app') THEN
                RETURN;
            END IF;

            GRANT USAGE ON SCHEMA public TO dentiva_app;
            GRANT SELECT, INSERT, UPDATE, DELETE
                ON ALL TABLES IN SCHEMA public TO dentiva_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dentiva_app;

            -- Future objects, whichever role creates them.
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dentiva_app;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT USAGE, SELECT ON SEQUENCES TO dentiva_app;
        END
        $$;
        """
    )


def downgrade() -> None:
    # Intentionally not revoking: this migration only ever ADDS privileges the
    # role was already meant to have, and revoking them mid-deploy would take
    # production down harder than the problem it fixes.
    pass
