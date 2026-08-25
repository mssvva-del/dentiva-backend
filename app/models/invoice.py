from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Invoice(UUIDPKMixin, TimestampMixin, Base):
    """A billing invoice for a practice (Platform Iter 1, Phase D).

    Mirrors a Stripe invoice. Created/updated from Stripe webhooks (invoice.paid /
    invoice.payment_failed). Kept locally so the clinic Billing tab and the admin
    revenue rollups don't have to call Stripe on every page load.
    """

    __tablename__ = "invoices"
    # One row per Stripe invoice — Stripe redelivers webhooks, so the handler
    # upserts on this. Partial unique (only when set) so non-Stripe/manual rows
    # with NULL stripe_invoice_id aren't constrained.
    __table_args__ = (
        Index(
            "uq_invoices_stripe_id", "stripe_invoice_id", unique=True,
            postgresql_where=text("stripe_invoice_id IS NOT NULL"),
        ),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    stripe_invoice_id: Mapped[str | None] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Running total refunded via the admin panel — partial refunds accumulate here,
    # and the remaining-amount check bounds the next refund (prevents over-refund
    # across multiple partials). amount == refunded → status 'refunded'.
    refunded_amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # 'draft' | 'open' | 'paid' | 'uncollectible' | 'void' (Stripe's vocabulary)
    # + ours: 'partially_refunded' | 'refunded' (admin refunds).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stripe's own hosted invoice page and PDF. Stored rather than fetched on
    # demand: a bookkeeper opening last quarter's receipt should not depend on
    # our Stripe key still being valid, and these URLs are stable per invoice.
    # Not PHI — an invoice line is a plan name and an amount, never a patient.
    hosted_invoice_url: Mapped[str | None] = mapped_column(Text)
    invoice_pdf_url: Mapped[str | None] = mapped_column(Text)
