"""practices.address + agent_settings (onboarding steps 1 & 5)

Platform Iter 1 — Phase B2. Adds the onboarding-collected fields that had no
home yet:
  * address        — nullable free-text street address (step 1)
  * agent_settings — JSONB {agent_name, voice, greeting} (step 5), nullable

Both nullable so existing practices are unaffected.

Revision ID: m8h9i0j1k2l3
Revises: l7g8h9i0j1k2
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'm8h9i0j1k2l3'
down_revision: str | None = 'l7g8h9i0j1k2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('practices', sa.Column('address', sa.Text(), nullable=True))
    op.add_column(
        'practices',
        sa.Column('agent_settings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('practices', 'agent_settings')
    op.drop_column('practices', 'address')
