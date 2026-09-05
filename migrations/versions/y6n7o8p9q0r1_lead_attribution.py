"""where a lead came from

The site already attaches first-touch attribution to every demo request —
landing page, referrer, UTM, the page the form was on — and sends it to an email
inbox. The leads table had no room for any of it, so the moment enquiries move
into the admin, "which channel produced this?" would have been unanswerable.

Revision ID: y6n7o8p9q0r1
Revises: x5m6n7o8p9q0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "y6n7o8p9q0r1"
down_revision = "x5m6n7o8p9q0"
branch_labels = None
depends_on = None

_COLS = ("landing_page", "referrer", "utm", "submitted_from")


def upgrade() -> None:
    for c in _COLS:
        op.add_column("leads", sa.Column(c, sa.Text(), nullable=True))


def downgrade() -> None:
    for c in reversed(_COLS):
        op.drop_column("leads", c)
