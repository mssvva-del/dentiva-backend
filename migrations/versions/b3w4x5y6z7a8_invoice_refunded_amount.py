"""invoices.refunded_amount_cents — partial-refund accounting (ADM3 reviewer #3)

Tracks the running refunded total so multiple partial refunds are bounded by the
REMAINING amount (not the full amount each time) and status flips to 'refunded'
exactly when the invoice is fully refunded.

Revision ID: b3w4x5y6z7a8
Revises: a2v3w4x5y6z7
Create Date: 2026-07-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b3w4x5y6z7a8'
down_revision: str | None = 'a2v3w4x5y6z7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("refunded_amount_cents", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    # Any invoice already marked refunded by the earlier code was a FULL refund.
    op.execute(
        "UPDATE invoices SET refunded_amount_cents = amount_cents "
        "WHERE status = 'refunded'"
    )


def downgrade() -> None:
    op.drop_column("invoices", "refunded_amount_cents")
