"""Add callback_requests table (persist create_callback_request)

Stores callback requests created by the voice agent so the practice sees
pending callbacks on the dashboard (urgent first). Patient identifiers are
encrypted (bytea) like the patients table. RLS + grants mirror patients.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-31
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'callback_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('call_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('patient_first_name', sa.LargeBinary(), nullable=True),
        sa.Column('phone', sa.LargeBinary(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('urgent', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_callbacks_practice_created',
        'callback_requests',
        ['practice_id', 'created_at'],
    )

    # Tenant isolation — same pattern as patients.
    op.execute("ALTER TABLE callback_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE callback_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON callback_requests
          USING (
            practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid
          )
        """
    )
    # Default privileges from the app_role migration should cover this, but grant
    # explicitly so the app role can read/write regardless of migration ordering.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON callback_requests TO dentiva_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON callback_requests")
    op.execute("ALTER TABLE callback_requests NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE callback_requests DISABLE ROW LEVEL SECURITY")
    op.drop_index('ix_callbacks_practice_created', table_name='callback_requests')
    op.drop_table('callback_requests')
