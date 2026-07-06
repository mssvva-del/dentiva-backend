from __future__ import annotations

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class PricingPlan(UUIDPKMixin, TimestampMixin, Base):
    """Editable MARKETING pricing shown on the site + managed in the admin panel.

    Deliberately SEPARATE from billing/plans.py (the internal metering/Stripe
    catalog): this is the public-facing price grid Sergio can change without a
    deploy. The site reads it via GET /api/pricing; admins edit it via the panel.
    Not PHI, not tenant-scoped — global config, no RLS.
    """

    __tablename__ = "pricing_plans"

    plan_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # after_hours…
    name: Mapped[str] = mapped_column(Text, nullable=False)
    monthly_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    annual_monthly_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # per-mo, annual
    soft_cap_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    overage_cents_per_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 'none' | 'basic' | 'full' — how much reactivation this tier includes.
    reactivation_level: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    per_location: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # highlight = "Most popular" badge on the site
    highlight: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class PlatformSetting(UUIDPKMixin, TimestampMixin, Base):
    """Singleton row of tweakable business knobs (referral rewards, etc). One row;
    admins edit it, no deploy needed. Extend with columns as new knobs appear."""

    __tablename__ = "platform_settings"

    # Referral: months of credit the referrer earns (100% of one month = 1), and the
    # invitee's first-month discount percent.
    referrer_reward_months: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1")
    invitee_discount_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15")
    annual_discount_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="16")
