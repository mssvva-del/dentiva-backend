"""alert_events — an operational alert that survives the process that raised it

Alerts lived in a ring buffer in memory. A redeploy erased them, and Railway
redeploys on every merge, so the window in which the most important signal we
have can vanish was opened several times a day. With two instances the health
endpoint reported whichever half the check happened to land on.

The alert that matters is page_not_delivered_urgent_callback: the clinic was
never told about a caller who said they were bleeding, and the agent had already
promised them otherwise.

Written by hand. Autogenerate wanted to drop fifteen indexes across live tables —
they are declared in migrations rather than on the models, so it read their
absence as intent. A generated migration is a draft, and this one would have
taken the indexes off patients, invoices and the reactivation queue in
production.

Revision ID: q8f9a0b1c2d3
Revises: p7e8f9a0b1c2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "q8f9a0b1c2d3"
down_revision = "p7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        # Codes, counts and ids only — never PHI, same contract as record_alert.
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Every read is "the last hour, newest first".
    op.create_index("ix_alert_events_created_at", "alert_events", ["created_at"])
    # The app role writes them; it reads them back through the health endpoint.
    op.execute("GRANT SELECT, INSERT, DELETE ON alert_events TO dentiva_app")


def downgrade() -> None:
    op.drop_index("ix_alert_events_created_at", table_name="alert_events")
    op.drop_table("alert_events")
