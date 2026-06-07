"""RBAC: extend users (is_internal, status, nullable practice_id) + dentiva_staff

Platform Iter 1 — Phase A2. Adds the columns RBAC needs and the internal-staff
table. practice_id becomes nullable so Dentiva internal users (no clinic) fit.

Revision ID: k6f7a8b9c0d1
Revises: j5e6f7a8b9c0
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'k6f7a8b9c0d1'
down_revision: str | None = 'j5e6f7a8b9c0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # users: new RBAC columns
    op.add_column(
        'users',
        sa.Column('is_internal', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'users',
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
    )
    # Internal staff have no clinic → practice_id must allow NULL.
    op.alter_column('users', 'practice_id', existing_type=postgresql.UUID(), nullable=True)

    # dentiva_staff — internal team roles (admin world). No RLS (not tenant-scoped).
    op.create_table(
        'dentiva_staff',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_dentiva_staff_user'),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON dentiva_staff TO dentiva_app")


def downgrade() -> None:
    op.drop_table('dentiva_staff')
    op.alter_column('users', 'practice_id', existing_type=postgresql.UUID(), nullable=False)
    op.drop_column('users', 'status')
    op.drop_column('users', 'is_internal')
