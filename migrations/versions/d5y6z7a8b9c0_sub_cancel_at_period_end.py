"""subscriptions.cancel_at_period_end — pending-cancel flag (ADM8)

Admin cancel endpoint sets it (service runs to period end, then Stripe stops
billing); the customer.subscription.updated webhook keeps it in sync; resume
clears it.

Revision ID: d5y6z7a8b9c0
Revises: c4x5y6z7a8b9
Create Date: 2026-07-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd5y6z7a8b9c0'
down_revision: str | None = 'c4x5y6z7a8b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False,
                  server_default="false"),
    )
    # Newest applied subscription.updated event (epoch). Guards against Stripe's
    # out-of-order webhook redelivery rolling state back (reviewer ADM8 #2).
    op.add_column(
        "subscriptions",
        sa.Column("last_stripe_event_ts", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "last_stripe_event_ts")
    op.drop_column("subscriptions", "cancel_at_period_end")
