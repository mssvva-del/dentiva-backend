"""clinic_notes — internal Dentiva-staff CRM notes on a clinic (ADM10)

OUR account commentary (sales/support), not patient PHI: no RLS, same as leads.
Reached only by internal staff via the audited /api/admin path.

Revision ID: f7a8b9c0d1e2
Revises: e6z7a8b9c0d1
Create Date: 2026-07-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f7a8b9c0d1e2'
down_revision: str | None = 'e6z7a8b9c0d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'clinic_notes',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('author_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('author_email', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['practice_id'], ['practices.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clinic_notes_practice_created', 'clinic_notes',
                    ['practice_id', 'created_at'])
    # No RLS (our business data, no tenant) — grant the app role DML.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON clinic_notes TO dentiva_app")


def downgrade() -> None:
    op.drop_index('ix_clinic_notes_practice_created', table_name='clinic_notes')
    op.drop_table('clinic_notes')
