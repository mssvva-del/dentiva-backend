from __future__ import annotations

from datetime import UTC, datetime, time

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.call import Call
from app.models.practice import Practice
from app.schemas.booking import DashboardToday

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/today", response_model=DashboardToday)
async def dashboard_today(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> DashboardToday:
    # NOTE: "today" is computed in UTC for Iter 1. Timezone-aware day boundaries
    # (using practice.timezone) are a Phase 2 refinement.
    today = datetime.now(UTC).date()
    day_start = datetime.combine(today, time.min, tzinfo=UTC)
    day_end = datetime.combine(today, time.max, tzinfo=UTC)

    calls_today = (
        await db.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= day_start)
            .where(Call.started_at <= day_end)
        )
    ).scalar_one()

    calls_missed = (
        await db.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= day_start)
            .where(Call.started_at <= day_end)
            .where(Call.status == "missed")
        )
    ).scalar_one()

    bookings_made_today = (
        await db.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.practice_id == practice.id)
            .where(Booking.created_at >= day_start)
            .where(Booking.created_at <= day_end)
        )
    ).scalar_one()

    upcoming_appointments_today = (
        await db.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.practice_id == practice.id)
            .where(Booking.appointment_at >= day_start)
            .where(Booking.appointment_at <= day_end)
            .where(Booking.status == "confirmed")
        )
    ).scalar_one()

    return DashboardToday(
        calls_today=calls_today,
        calls_answered_by_ai=calls_today - calls_missed,
        calls_missed=calls_missed,
        bookings_made_today=bookings_made_today,
        upcoming_appointments_today=upcoming_appointments_today,
    )
