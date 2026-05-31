"""Add waitlist_entries table (join_waitlist + cancellation backfill)

Stores patients who want an earlier slot. When a booking is cancelled, the
cancellation handler notifies the oldest waiting entry that a slot opened up.
Patient PII stays on the patients table (referenced by patient_id); this table
holds only operational preference fields. RLS + grants mirror patients.

Revision ID: g2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-05-31
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'g2b3c4d5e6f7'
down_revision: str | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'waitlist_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('call_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('procedure_type', sa.Text(), nullable=True),
        sa.Column('preferred_date', sa.Text(), nullable=True),
        sa.Column('preferred_time_window', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='waiting'),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id']),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_waitlist_practice_status_created',
        'waitlist_entries',
        ['practice_id', 'status', 'created_at'],
    )

    # Tenant isolation — same pattern as patients / callback_requests.
    op.execute("ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE waitlist_entries FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON waitlist_entries
          USING (
            practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid
          )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON waitlist_entries TO dentiva_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON waitlist_entries")
    op.execute("ALTER TABLE waitlist_entries NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE waitlist_entries DISABLE ROW LEVEL SECURITY")
    op.drop_index('ix_waitlist_practice_status_created', table_name='waitlist_entries')
    op.drop_table('waitlist_entries')
