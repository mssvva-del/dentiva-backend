"""what the practice's own system said when we last wrote to it

A cancellation their API refuses reaches us as "pms_error" in a log nobody
reads. Three of them sat in a live clinic's calendar this morning while every
screen we have showed the appointments as cancelled — the patient gone, the
chair still blocked, and no way to see it or try again.

The column holds the outcome of the last write-back: NULL when the two calendars
agree, otherwise the refusal in the practice software's own words.

Revision ID: w4l5m6n7o8p9
Revises: v3k4l5m6n7o8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "w4l5m6n7o8p9"
down_revision = "v3k4l5m6n7o8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("pms_sync_status", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "pms_sync_status")
