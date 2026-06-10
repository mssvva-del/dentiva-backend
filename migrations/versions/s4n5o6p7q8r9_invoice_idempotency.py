"""invoices.stripe_invoice_id partial-unique (Stripe webhook idempotency)

Audit finding (CURRENT_STATE_AUDIT §2): invoice.paid / payment_failed inserted a
new invoices row on every delivery. Stripe retries webhooks, so a retry created
DUPLICATE invoice rows. This partial-unique index (only when stripe_invoice_id is
set) lets the handler upsert (ON CONFLICT) instead of blind-insert — one row per
Stripe invoice. Manual/non-Stripe rows (NULL id) are unconstrained.

Revision ID: s4n5o6p7q8r9
Revises: r3m4n5o6p7q8
Create Date: 2026-06-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = 's4n5o6p7q8r9'
down_revision: str | None = 'r3m4n5o6p7q8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_invoices_stripe_id ON invoices (stripe_invoice_id) "
        "WHERE stripe_invoice_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_invoices_stripe_id")
