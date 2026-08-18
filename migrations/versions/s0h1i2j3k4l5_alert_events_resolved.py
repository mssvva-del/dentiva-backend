"""alert_events.resolved_at — a reported problem can be closed

The Report button on a broken screen writes into alert_events. Without a way to
close one, the list is every problem ever reported with no way to tell which
were dealt with, and an operator opening it on week three reads it as noise —
which is the same as not having built the button.

Nullable with no default: NULL means open, and every alert that already exists
is open, which is the honest reading of "nobody has looked at it yet".

Revision ID: s0h1i2j3k4l5
Revises: r9g0h1i2j3k4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s0h1i2j3k4l5"
down_revision = "r9g0h1i2j3k4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_events",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "alert_events",
        # Who closed it. A name, not an id: the row outlives staff accounts, and
        # "resolved by a user that no longer exists" answers nothing.
        sa.Column("resolved_by", sa.Text(), nullable=True),
    )
    # The list is "open reports, newest first". A partial index keeps that read
    # cheap without carrying every resolved row forever.
    op.create_index(
        "ix_alert_events_open",
        "alert_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    # The app role writes alerts; the admin screen resolves them.
    op.execute("GRANT UPDATE ON alert_events TO dentiva_app")


def downgrade() -> None:
    op.drop_index("ix_alert_events_open", table_name="alert_events")
    op.drop_column("alert_events", "resolved_by")
    op.drop_column("alert_events", "resolved_at")
