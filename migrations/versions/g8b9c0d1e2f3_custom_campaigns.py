"""Custom clinic-authored campaigns (category/context/channels/attestation)

Adds to reactivation_campaigns:
  category            treatment|marketing (marketing = promo, needs attestation)
  custom_context      the clinic's own message/direction (SMS + voice context)
  channels            sms|voice|both
  consent_attested_by / consent_attested_at — PEWC attestation for marketing

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'g8b9c0d1e2f3'
down_revision: str | None = 'f7a8b9c0d1e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reactivation_campaigns",
                  sa.Column("category", sa.Text(), nullable=False,
                            server_default="treatment"))
    op.add_column("reactivation_campaigns",
                  sa.Column("custom_context", sa.Text(), nullable=True))
    op.add_column("reactivation_campaigns",
                  sa.Column("channels", sa.Text(), nullable=False,
                            server_default="sms"))
    op.add_column("reactivation_campaigns",
                  sa.Column("consent_attested_by",
                            postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("reactivation_campaigns",
                  sa.Column("consent_attested_at",
                            sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("reactivation_campaigns", "consent_attested_at")
    op.drop_column("reactivation_campaigns", "consent_attested_by")
    op.drop_column("reactivation_campaigns", "channels")
    op.drop_column("reactivation_campaigns", "custom_context")
    op.drop_column("reactivation_campaigns", "category")
