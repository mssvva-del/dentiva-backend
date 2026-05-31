"""Add patients.sms_opt_out (TCPA opt-out via STOP keyword)

Set when a patient texts STOP; cleared on START. All outbound SMS senders skip
opted-out patients.

Revision ID: h3c4d5e6f7a8
Revises: g2b3c4d5e6f7
Create Date: 2026-05-31
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'h3c4d5e6f7a8'
down_revision: str | None = 'g2b3c4d5e6f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'patients',
        sa.Column(
            'sms_opt_out', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column('patients', 'sms_opt_out')
