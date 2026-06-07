"""feature_flags table (Phase E — admin feature toggles)

Global (practice_id NULL) or per-practice flags, managed from the admin panel.
No RLS — operational config, scoped by explicit query. Partial unique indexes
keep one global row and one per-practice row per flag key.

Revision ID: q2l3m4n5o6p7
Revises: p1k2l3m4n5o6
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'q2l3m4n5o6p7'
down_revision: str | None = 'p1k2l3m4n5o6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'feature_flags',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('practice_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('flag_key', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feature_flags_key', 'feature_flags', ['flag_key'])
    # One global row per key (practice_id IS NULL) ...
    op.execute(
        "CREATE UNIQUE INDEX uq_feature_flag_global ON feature_flags (flag_key) "
        "WHERE practice_id IS NULL"
    )
    # ... and one override row per (practice, key).
    op.execute(
        "CREATE UNIQUE INDEX uq_feature_flag_practice ON feature_flags "
        "(practice_id, flag_key) WHERE practice_id IS NOT NULL"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON feature_flags TO dentiva_app")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_feature_flag_practice")
    op.execute("DROP INDEX IF EXISTS uq_feature_flag_global")
    op.drop_index('ix_feature_flags_key', table_name='feature_flags')
    op.drop_table('feature_flags')
