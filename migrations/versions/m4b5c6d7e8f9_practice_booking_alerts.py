"""practices.booking_alerts_enabled — text the clinic when the AI books

Until a PMS sync exists, an AI booking is only visible in our dashboard, and a
dental front desk does not watch a dashboard. Default TRUE so existing practices
start getting the alert; a clinic that finds it noisy turns it off in Settings.

Revision ID: m4b5c6d7e8f9
Revises: l3a4b5c6d7e8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "m4b5c6d7e8f9"
down_revision = "l3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "practices",
        sa.Column(
            "booking_alerts_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("practices", "booking_alerts_enabled")
