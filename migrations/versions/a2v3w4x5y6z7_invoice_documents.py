"""Store the invoice's hosted page and PDF so a clinic can print its receipt

The billing screen listed invoices as date + amount + status and stopped there.
A dental practice has a bookkeeper who needs the actual document, and Stripe
already puts both a hosted page and a PDF on every invoice object — we were
receiving them in the webhook and dropping them, so the only way to get a
receipt was to ask us for one.

Revision ID: a2v3w4x5y6z7
Revises: z1u2v3w4x5y6
Create Date: 2026-08-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a2v3w4x5y6z7'
down_revision: str | None = 'z1u2v3w4x5y6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: invoices recorded before this migration have no stored links, and
    # an invoice created outside Stripe (a manual adjustment) may never have one.
    op.add_column("invoices", sa.Column("hosted_invoice_url", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("invoice_pdf_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "invoice_pdf_url")
    op.drop_column("invoices", "hosted_invoice_url")
