"""Add practices.transfer_phone_number (transfer_to_human destination)

Where transfer_to_human routes the live call. E.164; falls back to phone_number
when unset.

Revision ID: j5e6f7a8b9c0
Revises: i4d5e6f7a8b9
Create Date: 2026-06-01
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'j5e6f7a8b9c0'
down_revision: str | None = 'i4d5e6f7a8b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'practices', sa.Column('transfer_phone_number', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('practices', 'transfer_phone_number')
