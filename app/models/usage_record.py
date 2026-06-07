from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class UsageRecord(UUIDPKMixin, TimestampMixin, Base):
    """Per-practice, per-billing-period usage roll-up (Platform Iter 1, Phase D).

    One row per (practice, billing period). Incremented on every call_ended
    (minutes + call count) so the clinic sees live usage-vs-limit and overage can
    be computed at period close. SMS count is tracked too for cost analysis.

    Minutes are NUMERIC (not float) so accumulation stays exact across thousands
    of calls — money is derived from this, so precision matters.
    """

    __tablename__ = "usage_records"
    # One row per (practice, period) — matches the migration's unique index and
    # backs the metering upsert (ON CONFLICT). Declared here so create_all (tests)
    # builds the same constraint the migration does.
    __table_args__ = (
        Index("uq_usage_practice_period", "practice_id", "period_start", unique=True),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minutes_used: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0"
    )
    calls_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sms_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Computed at period close (minutes beyond the plan allowance). 0 until then.
    overage_minutes: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0"
    )
