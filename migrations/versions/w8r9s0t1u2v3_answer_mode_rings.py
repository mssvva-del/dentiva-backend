"""practices.answer_mode + rings_before_ai (ring-count / AI answer config)

Per-clinic call-routing config (see RING_COUNT_ASSESSMENT.md): how the AI fronts
the phone (full_time / overflow / after_hours) and how many rings the clinic line
waits before forwarding to the AI. Drives the tariff, the dashboard setting, and
the onboarding forwarding instruction.

Revision ID: w8r9s0t1u2v3
Revises: v7q8r9s0t1u2
Create Date: 2026-06-26
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'w8r9s0t1u2v3'
down_revision: str | None = 'v7q8r9s0t1u2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practices",
        sa.Column("answer_mode", sa.Text(), nullable=False, server_default="overflow"),
    )
    op.add_column(
        "practices",
        sa.Column("rings_before_ai", sa.Integer(), nullable=False, server_default="3"),
    )


def downgrade() -> None:
    op.drop_column("practices", "rings_before_ai")
    op.drop_column("practices", "answer_mode")
