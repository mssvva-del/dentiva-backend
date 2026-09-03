"""notes on an appointment, and notes on a patient

Callers say things the appointment row has no room for: which tooth, who is
driving them, that they are pregnant, that the last cleaning hurt. Today all of
it lives inside a transcript nobody reads before the patient walks in — the
front desk sees "Cleaning, 9:00" and knows nothing else.

Two columns, two different lifetimes. The booking note is about THIS visit and
is written by the agent while the caller is talking. The patient note is about
the person and outlives every appointment, so the front desk owns it and types
into it after they meet them.

Both are Fernet-encrypted: they are free text a patient dictated, which is the
same reason callback reasons and waitlist notes were encrypted before them.

Revision ID: v3k4l5m6n7o8
Revises: u2j3k4l5m6n7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "v3k4l5m6n7o8"
down_revision = "u2j3k4l5m6n7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("notes", sa.LargeBinary(), nullable=True))
    op.add_column("patients", sa.Column("notes", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "notes")
    op.drop_column("bookings", "notes")
