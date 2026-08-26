"""The group's own id for a location, so an import can be run twice

A DSO onboards from a spreadsheet of their own locations, and that spreadsheet
gets re-sent: a corrected timezone, ten more practices, a retry after half the
rows failed. Without a stable key from THEIR side, the second run creates a
second copy of every clinic — each with its own Clerk organisation, its own
calls, and no way to tell which one the phone number belongs to.

Nullable because clinics that sign themselves up have no such id and never will.
Unique because that is the entire point: it is what makes a re-import an update.

Revision ID: u2j3k4l5m6n7
Revises: t1i2j3k4l5m6
Create Date: 2026-08-26
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'u2j3k4l5m6n7'
down_revision: str | None = 't1i2j3k4l5m6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("practices", sa.Column("external_ref", sa.Text(), nullable=True))
    # Partial: many practices legitimately have no external ref, and a plain
    # unique index would collapse all of them into one allowed NULL on some
    # engines. Postgres permits repeated NULLs, but stating the intent here keeps
    # the constraint honest if this is ever ported.
    op.create_index(
        "ix_practices_external_ref",
        "practices",
        ["external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_practices_external_ref", table_name="practices")
    op.drop_column("practices", "external_ref")
