"""Add appointment-reminder idempotency columns to bookings

The reminder scheduler (app/services/reminders.py) sends an SMS ~24h and ~2h
before an appointment. These two nullable timestamps record when each reminder
was sent so the loop never double-sends.

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-05-31
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f1a2b3c4d5e6'
down_revision: str | None = 'e5f6a7b8c9d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'bookings',
        sa.Column('reminder_24h_sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'bookings',
        sa.Column('reminder_2h_sent_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('bookings', 'reminder_2h_sent_at')
    op.drop_column('bookings', 'reminder_24h_sent_at')
