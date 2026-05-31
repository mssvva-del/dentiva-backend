"""Add calls.emergency_active for the programmatic emergency lock

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL with a server default so existing rows backfill to False safely.
    op.add_column(
        'calls',
        sa.Column(
            'emergency_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('calls', 'emergency_active')
