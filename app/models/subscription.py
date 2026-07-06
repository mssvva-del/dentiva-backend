from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Subscription(UUIDPKMixin, TimestampMixin, Base):
    """A practice's billing plan (Platform Iter 1, Phase D).

    One active subscription per practice. Mirrors the Stripe subscription but our
    row is authoritative for entitlement (included_minutes, status) so billing
    works even before Stripe is wired, and so super_admin can run pilots / custom
    deals without a Stripe object.
    """

    __tablename__ = "subscriptions"

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text)
    # 'starter' | 'practice' | 'group' (see app/billing/plans.py)
    plan: Mapped[str] = mapped_column(Text, nullable=False)
    # 'monthly' | 'annual'
    billing_cycle: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="monthly"
    )
    # see plans.SUBSCRIPTION_STATUSES (trialing|active|pilot|past_due|suspended|cancelled)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="trialing")
    # Entitlement snapshot — copied from the plan at creation but OVERRIDABLE per
    # client by super_admin (custom deals), which is why it's stored, not derived.
    included_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Recurring revenue for this practice, in cents (after any custom discount).
    # Used by the admin MRR/margin rollups. Override of the plan default allowed.
    mrr_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Per-client overrides (NULL = use the plan default from the catalog).
    overage_cents_per_min: Mapped[int | None] = mapped_column(Integer)
    setup_fee_cents: Mapped[int | None] = mapped_column(Integer)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Pending cancellation: service continues until current_period_end, then Stripe
    # stops billing. Set by the admin cancel endpoint; kept in sync by the
    # customer.subscription.updated webhook; cleared by resume.
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Epoch `created` of the newest customer.subscription.updated event we applied.
    # Stripe redelivers failed webhooks OUT OF ORDER — a stale retry must not roll
    # status/cancel_at_period_end back to an older state (reviewer ADM8 #2).
    last_stripe_event_ts: Mapped[int | None] = mapped_column(BigInteger)
