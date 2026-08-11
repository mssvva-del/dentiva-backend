"""practices.is_canary — a clinic that exists only to be tested against

Monitoring can watch health endpoints from outside and learn quite a lot, but it
cannot answer the question that matters: does a call still end in a booking? The
only honest way to know is to make one — and a synthetic appointment in a live
practice's calendar is worse than no monitoring, because a receptionist has to
explain a patient who does not exist.

So one practice is marked as existing for that purpose. Everything that counts,
bills, or reports on clinics has to skip it, and the flag is what makes that
possible to write and to grep for.

The column is nullable-with-a-default rather than NOT NULL: this table is small
today, but a NOT NULL rewrite on a table the voice agent reads on every call is a
lock nobody needs for a boolean.

Revision ID: p7e8f9a0b1c2
Revises: o6d7e8f9a0b1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p7e8f9a0b1c2"
down_revision = "o6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "practices",
        sa.Column(
            "is_canary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Partial index: there is one canary, and it is looked up by that fact alone.
    op.create_index(
        "ix_practices_canary",
        "practices",
        ["is_canary"],
        unique=False,
        postgresql_where=sa.text("is_canary"),
    )


def downgrade() -> None:
    op.drop_index("ix_practices_canary", table_name="practices")
    op.drop_column("practices", "is_canary")
