"""Add practices.reminders_enabled (per-practice reminder toggle)

The global REMINDERS_ENABLED env starts the scheduler loop; this column lets an
individual practice opt in/out. Defaults true so reminders work once enabled
globally without extra per-practice setup.

Revision ID: i4d5e6f7a8b9
Revises: h3c4d5e6f7a8
Create Date: 2026-05-31
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'i4d5e6f7a8b9'
down_revision: str | None = 'h3c4d5e6f7a8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'practices',
        sa.Column(
            'reminders_enabled', sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column('practices', 'reminders_enabled')
