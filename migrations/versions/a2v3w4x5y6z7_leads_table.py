"""leads table — sales lead inbox (marketing-site demo form → admin)

OUR business data (a prospect practice), not clinic PHI: no practice_id, no RLS.
Read/managed only by Dentiva admin staff with MANAGE_LEADS.

Revision ID: a2v3w4x5y6z7
Revises: z1u2v3w4x5y6
Create Date: 2026-07-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = 'a2v3w4x5y6z7'
down_revision: str | None = 'z1u2v3w4x5y6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("clinic_name", sa.Text()),
        sa.Column("message", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False, server_default="site"),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_leads_status_created", "leads", ["status", "created_at"])
    # No RLS on leads (our business data, no tenant) — grant the app role DML so the
    # public lead-form endpoint and the admin inbox can use it.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON leads TO dentiva_app")


def downgrade() -> None:
    op.drop_index("ix_leads_status_created", table_name="leads")
    op.drop_table("leads")
