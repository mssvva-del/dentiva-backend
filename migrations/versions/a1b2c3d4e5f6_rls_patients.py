"""rls_patients

Enable Row-Level Security tenant isolation on the patients table.

Revision ID: a1b2c3d4e5f6
Revises: 719d6c7f3de2
Create Date: 2026-05-29 23:55:00.000000
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '719d6c7f3de2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE patients ENABLE ROW LEVEL SECURITY")
    # FORCE so the policy applies even to the table owner (the app connects as
    # the owner role in local mode). Belt-and-suspenders tenant isolation.
    op.execute("ALTER TABLE patients FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON patients
          USING (
            practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid
          )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON patients")
    op.execute("ALTER TABLE patients NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE patients DISABLE ROW LEVEL SECURITY")
