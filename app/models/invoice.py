from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
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

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    stripe_invoice_id: Mapped[str | None] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 'draft' | 'open' | 'paid' | 'uncollectible' | 'void' (Stripe's vocabulary)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
