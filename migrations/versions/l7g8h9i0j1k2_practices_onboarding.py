"""practices: onboarding_step + status + stripe_customer_id (Phase B0)

Platform Iter 1 — Phase B (registration + onboarding). Adds the columns the
onboarding wizard and lifecycle/billing need:

  * onboarding_step  — wizard progress; 0 = complete/live, 1..N = step in progress
  * status           — onboarding|trial|pilot|active|suspended|cancelled
  * stripe_customer_id — set in Phase D at checkout; nullable until billed

Existing practices keep working: onboarding_step defaults to 0 (already set up)
and status defaults to 'active', so the demo practice is NOT bounced into the
wizard. New practices created by the Clerk webhook (B1) explicitly start at
step 1 / status 'onboarding'.

NOTE: the spec also lists `transfer_to_human_number`; that already exists as
`practices.transfer_phone_number` (added in V4-1 / j5e6f7a8b9c0), so it is NOT
re-added here to avoid a duplicate column.

Revision ID: l7g8h9i0j1k2
Revises: k6f7a8b9c0d1
Create Date: 2026-06-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'l7g8h9i0j1k2'
down_revision: str | None = 'k6f7a8b9c0d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Wizard progress. server_default '0' keeps existing practices "done".
    op.add_column(
        'practices',
        sa.Column('onboarding_step', sa.Integer(), nullable=False, server_default='0'),
    )
    # Lifecycle status. Plain text (not an enum) so super_admin can add states
    # without a migration; validated in the service layer. Existing rows -> active.
    op.add_column(
        'practices',
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
    )
    # Stripe customer handle — populated in Phase D. Nullable until first billed.
    op.add_column(
        'practices',
        sa.Column('stripe_customer_id', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('practices', 'stripe_customer_id')
    op.drop_column('practices', 'status')
    op.drop_column('practices', 'onboarding_step')
