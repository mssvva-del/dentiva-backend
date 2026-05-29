"""app_role

Create a dedicated non-superuser application role (``dentiva_app``) that is
subject to Row-Level Security. The bootstrap ``dentiva`` superuser bypasses RLS
(rolbypassrls=t), so the app MUST connect as this role for tenant isolation to
actually enforce.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-30 00:10:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dentiva_app') THEN
                CREATE ROLE dentiva_app LOGIN PASSWORD 'dentiva_app' NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO dentiva_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dentiva_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dentiva_app"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM dentiva_app"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM dentiva_app"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM dentiva_app")
    op.execute("DROP ROLE IF EXISTS dentiva_app")
