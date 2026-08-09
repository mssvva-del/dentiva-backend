"""Usage metering (Platform Iter 1, Phase D).

Every completed call adds its minutes to the practice's usage_record for the
current billing period. This is what powers the clinic's "minutes used vs limit"
display and the overage calculation at period close.

Design notes:
  * Minutes are accumulated as exact Decimals (money derives from them).
  * The period is the subscription's current period when set, else the calendar
    month — so metering works even before a practice has a subscription.
  * The upsert keys on (practice_id, period_start) (unique index), so concurrent
    call_ended events can't create duplicate rows.
  * usage_records is RLS-protected, so we bind the tenant before writing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_tenant
from app.models.subscription import Subscription
from app.models.usage_record import UsageRecord
from app.observability.alerts import record_alert


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[first of this month 00:00, first of next month 00:00) in UTC."""
    start = now.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


async def current_period(
    session: AsyncSession, practice_id: uuid.UUID, *, now: datetime
) -> tuple[datetime, datetime]:
    """The billing period to meter into: the subscription's window, else month."""
    sub = (
        await session.execute(
            select(Subscription).where(Subscription.practice_id == practice_id)
        )
    ).scalar_one_or_none()
    if sub and sub.current_period_start and sub.current_period_end:
        return sub.current_period_start, sub.current_period_end
    return _month_bounds(now)


async def record_call_usage(
    session: AsyncSession,
    practice_id: uuid.UUID,
    duration_seconds: int | None,
    *,
    now: datetime | None = None,
) -> None:
    """Add one call's minutes to the practice's current-period usage row.

    No-op if duration is missing/zero (we still count the call). Idempotency is
    NOT attempted per-call (a redelivered call_ended would double-count); call
    sites should only invoke this once per call. Safe to call within the
    call_ended handler's session before commit.
    """
    now = now or datetime.now(UTC)
    minutes = Decimal(max(0, duration_seconds or 0)) / Decimal(60)
    period_start, period_end = await current_period(session, practice_id, now=now)

    # Bind tenant for the RLS-protected usage_records table.
    await set_tenant(session, practice_id)

    # Upsert: create the period row or add to the running totals.
    stmt = (
        pg_insert(UsageRecord)
        .values(
            id=uuid.uuid4(),
            practice_id=practice_id,
            period_start=period_start,
            period_end=period_end,
            minutes_used=minutes,
            calls_count=1,
        )
        .on_conflict_do_update(
            index_elements=["practice_id", "period_start"],
            set_={
                "minutes_used": UsageRecord.minutes_used + minutes,
                "calls_count": UsageRecord.calls_count + 1,
                "updated_at": now,
            },
        )
    )
    total = (await session.execute(
        stmt.returning(UsageRecord.minutes_used)
    )).scalar_one()

    # Crossing the included minutes is the moment somebody needs to know, and it
    # is invisible otherwise: nothing breaks, calls keep being answered, and the
    # first sign is the invoice. Alert on the crossing itself — comparing the
    # total before and after this call means exactly one alert per period, with
    # no state to keep and nothing to reset when the period rolls over.
    sub = (await session.execute(
        select(Subscription).where(Subscription.practice_id == practice_id)
    )).scalar_one_or_none()
    cap = sub.included_minutes if sub else None
    if cap and (total - minutes) < cap <= total:
        record_alert(
            "usage_cap_exceeded",
            f"practice {practice_id} passed {cap} included minutes this period",
        )


async def record_sms_usage(
    session: AsyncSession, practice_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Increment the SMS counter for the practice's current period (cost tracking)."""
    now = now or datetime.now(UTC)
    period_start, period_end = await current_period(session, practice_id, now=now)
    await set_tenant(session, practice_id)
    stmt = (
        pg_insert(UsageRecord)
        .values(
            id=uuid.uuid4(), practice_id=practice_id,
            period_start=period_start, period_end=period_end, sms_count=1,
        )
        .on_conflict_do_update(
            index_elements=["practice_id", "period_start"],
            set_={"sms_count": UsageRecord.sms_count + 1, "updated_at": now},
        )
    )
    await session.execute(stmt)
