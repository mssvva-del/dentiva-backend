"""Add knowledge_base JSONB column to practices table.

Stores per-practice clinic information (providers, appointment types,
insurances, policies, emergency protocol) as a free-form JSONB blob.
Additive only — no breaking changes, nullable so existing rows are unaffected.

Revision ID: u6p7q8r9s0t1
Revises: t5o6p7q8r9s0
Create Date: 2026-06-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'u6p7q8r9s0t1'
down_revision: str | None = 't5o6p7q8r9s0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable JSONB — existing practices default to NULL (no knowledge base yet).
    # The application treats NULL and {} identically (empty knowledge base).
    op.add_column(
        'practices',
        sa.Column('knowledge_base', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('practices', 'knowledge_base')
