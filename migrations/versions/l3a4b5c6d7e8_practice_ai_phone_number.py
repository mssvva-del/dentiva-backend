"""practices.ai_phone_number — a Dentovox number per clinic (NUM-1)

The number a clinic forwards its own line to was a single global env var
(RETELL_FROM_NUMBER), so every practice shared one. Inbound routing has no way to
tell two clinics apart from a shared number, which is why a second clinic's calls
could not be attributed. This gives each practice its own, and UNIQUE makes the
routing lookup unambiguous by construction.

NULL means "not provisioned yet" — those practices keep using the global number,
so nothing changes for the existing single-tenant pilot.

Revision ID: l3a4b5c6d7e8
Revises: j1e2f3a4b5c6
Create Date: 2026-07-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'l3a4b5c6d7e8'
down_revision: str | None = 'j1e2f3a4b5c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("practices", sa.Column("ai_phone_number", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_practices_ai_phone_number", "practices", ["ai_phone_number"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_practices_ai_phone_number", "practices", type_="unique")
    op.drop_column("practices", "ai_phone_number")
