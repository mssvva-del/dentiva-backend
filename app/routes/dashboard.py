from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.call import Call
from app.models.practice import Practice
from app.schemas.booking import (
    BriefingResponse,
    CallsByHourResponse,
    ConversionResponse,
    DailyStats,
    DashboardToday,
    HourlyCount,
    ProcedureCount,
    ROIResponse,
    WeeklyStatsResponse,
    WeeklyTotals,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

settings = get_settings()


async def _generate_briefing(stats: dict, peak_hours: list[dict]) -> tuple[str, bool]:
    """Call Groq to generate a natural-language daily briefing.

    Returns (text, ai_generated) — ai_generated is False when the fallback fires.
    """
    peak_str = (
        ", ".join(
            f"{h['hour']}:00–{h['hour'] + 1}:00 ({h['count']} calls)"
            for h in peak_hours[:3]
        )
        if peak_hours
        else "no peak data"
    )

    prompt = (
        f"Write a 2-sentence daily briefing for a dental practice manager. "
        f"Be concise and professional. Today's stats: "
        f"{stats['calls_today']} total calls, "
        f"{stats['calls_answered_by_ai']} answered by AI, "
        f"{stats['calls_missed']} missed, "
        f"{stats['bookings_made_today']} new bookings, "
        f"{stats['upcoming_appointments_today']} upcoming appointments today. "
        f"Peak call hours: {peak_str}. "
        f"Start with 'Today' and do not use bullet points."
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{settings.groq_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.llm_model_fast,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.4,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return text, True
    except Exception:
        # Fallback: plain-text briefing without AI.
        fallback = (
            f"Today the AI receptionist handled {stats['calls_answered_by_ai']} of "
            f"{stats['calls_today']} calls, creating {stats['bookings_made_today']} new "
            f"bookings. {stats['upcoming_appointments_today']} appointments are scheduled"
            f" for today."
        )
        return fallback, False


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


@router.get("/briefing", response_model=BriefingResponse)
async def get_dashboard_briefing(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> BriefingResponse:
    today = datetime.now(UTC).date()
    day_start = datetime.combine(today, time.min, tzinfo=UTC)
    day_end = datetime.combine(today, time.max, tzinfo=UTC)

    # ── Today stats (same queries as /today) ─────────────────────────────────
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

    stats = DashboardToday(
        calls_today=calls_today,
        calls_answered_by_ai=calls_today - calls_missed,
        calls_missed=calls_missed,
        bookings_made_today=bookings_made_today,
        upcoming_appointments_today=upcoming_appointments_today,
    )

    # ── Peak hours (top 3 busiest hours today) ───────────────────────────────
    # Report hours in the practice's local timezone, not UTC — otherwise a 5pm
    # call shows up as "22:00" and the briefing reads as if patients call at
    # night. `started_at AT TIME ZONE <tz>` converts the timestamptz to local.
    local_hour = func.extract(
        "hour", func.timezone(practice.timezone, Call.started_at)
    )
    peak_rows = (
        await db.execute(
            select(
                local_hour.label("hour"),
                func.count().label("count"),
            )
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= day_start)
            .where(Call.started_at <= day_end)
            .group_by(local_hour)
            .order_by(func.count().desc())
            .limit(3)
        )
    ).all()

    peak_hours = [{"hour": int(row.hour), "count": row.count} for row in peak_rows]

    # ── AI briefing ───────────────────────────────────────────────────────────
    briefing_text, ai_generated = await _generate_briefing(stats.model_dump(), peak_hours)

    return BriefingResponse(
        text=briefing_text,
        stats=stats,
        peak_hours=peak_hours,
        generated_at=datetime.now(UTC),
        ai_generated=ai_generated,
    )


@router.get("/weekly", response_model=WeeklyStatsResponse)
async def get_weekly_stats(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> WeeklyStatsResponse:
    """Return per-day call and booking stats for the last 7 calendar days (today inclusive).

    TODO: Group by DATE(started_at AT TIME ZONE practice.timezone) once timezone-aware
    grouping is added. Currently uses UTC dates.
    """
    today = datetime.now(UTC).date()
    # Build the list of the last 7 dates: oldest first, today last.
    date_range: list[date] = [today - timedelta(days=i) for i in range(6, -1, -1)]
    window_start = datetime.combine(date_range[0], time.min, tzinfo=UTC)
    window_end = datetime.combine(today, time.max, tzinfo=UTC)

    # ── Per-day call counts ──────────────────────────────────────────────────
    _day_trunc = func.date_trunc("day", Call.started_at)
    call_day_rows = (
        await db.execute(
            select(
                _day_trunc.label("day"),
                func.count().label("calls_total"),
                func.sum(
                    case((Call.status == "missed", 1), else_=0)
                ).label("calls_missed"),
                func.sum(
                    case((Call.status == "completed", 1), else_=0)
                ).label("calls_answered"),
                func.coalesce(
                    func.avg(
                        case(
                            (Call.status == "completed", Call.duration_seconds),
                            else_=None,
                        )
                    ),
                    0,
                ).label("avg_duration"),
            )
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= window_start)
            .where(Call.started_at <= window_end)
            .group_by(text("1"))
        )
    ).all()

    # Build a lookup keyed by date.
    call_map: dict[date, dict] = {}
    for row in call_day_rows:
        d: date = row.day.date()
        call_map[d] = {
            "calls_total": int(row.calls_total),
            "calls_missed": int(row.calls_missed or 0),
            "calls_answered": int(row.calls_answered or 0),
            "avg_duration": int(round(float(row.avg_duration or 0))),
        }

    # ── Per-day booking counts ───────────────────────────────────────────────
    booking_day_rows = (
        await db.execute(
            select(
                func.date_trunc("day", Booking.created_at).label("day"),
                func.count().label("bookings_created"),
            )
            .where(Booking.practice_id == practice.id)
            .where(Booking.created_at >= window_start)
            .where(Booking.created_at <= window_end)
            .group_by(text("1"))
        )
    ).all()

    booking_map: dict[date, int] = {
        row.day.date(): int(row.bookings_created) for row in booking_day_rows
    }

    # ── Assemble daily rows (fill gaps with zeros) ───────────────────────────
    days: list[DailyStats] = []
    for d in date_range:
        c = call_map.get(
            d, {"calls_total": 0, "calls_missed": 0, "calls_answered": 0, "avg_duration": 0}
        )
        days.append(
            DailyStats(
                date=d,
                calls_total=c["calls_total"],
                calls_answered_by_ai=c["calls_answered"],
                calls_missed=c["calls_missed"],
                bookings_created=booking_map.get(d, 0),
                avg_duration_seconds=c["avg_duration"],
            )
        )

    # ── Totals ───────────────────────────────────────────────────────────────
    total_calls = sum(d.calls_total for d in days)
    total_answered = sum(d.calls_answered_by_ai for d in days)
    total_missed = sum(d.calls_missed for d in days)
    total_bookings = sum(d.bookings_created for d in days)
    ai_answer_rate = round(total_answered / total_calls, 3) if total_calls else 0.0

    return WeeklyStatsResponse(
        days=days,
        totals=WeeklyTotals(
            calls_total=total_calls,
            calls_answered_by_ai=total_answered,
            calls_missed=total_missed,
            bookings_created=total_bookings,
            ai_answer_rate=ai_answer_rate,
        ),
    )


@router.get("/calls-by-hour", response_model=CallsByHourResponse)
async def get_calls_by_hour(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> CallsByHourResponse:
    """Return call volume by hour-of-day (0–23) aggregated over the last 30 days.

    Used for a heatmap showing peak call hours on the dashboard.
    """
    today = datetime.now(UTC).date()
    window_start = datetime.combine(today - timedelta(days=29), time.min, tzinfo=UTC)
    window_end = datetime.combine(today, time.max, tzinfo=UTC)

    # Bucket by hour-of-day in the practice's local timezone so the heatmap
    # reflects real business hours, not a UTC-shifted version.
    local_hour = func.extract(
        "hour", func.timezone(practice.timezone, Call.started_at)
    )
    rows = (
        await db.execute(
            select(
                local_hour.label("hour"),
                func.count().label("count"),
            )
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= window_start)
            .where(Call.started_at <= window_end)
            .group_by(local_hour)
        )
    ).all()

    # Build a lookup and fill all 24 hours.
    hour_map: dict[int, int] = {int(row.hour): int(row.count) for row in rows}  # type: ignore[call-overload]
    hours: list[HourlyCount] = [
        HourlyCount(hour=h, count=hour_map.get(h, 0)) for h in range(24)
    ]

    peak = max(hours, key=lambda x: x.count, default=HourlyCount(hour=0, count=0))

    return CallsByHourResponse(
        hours=hours,
        peak_hour=peak.hour,
        peak_count=peak.count,
    )


@router.get("/conversion", response_model=ConversionResponse)
async def get_conversion(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> ConversionResponse:
    """Return booking conversion funnel stats for the last 30 days."""
    period_days = 30
    today = datetime.now(UTC).date()
    window_start = datetime.combine(today - timedelta(days=period_days - 1), time.min, tzinfo=UTC)
    window_end = datetime.combine(today, time.max, tzinfo=UTC)

    # ── Call aggregates ──────────────────────────────────────────────────────
    call_agg = (
        await db.execute(
            select(
                func.count().label("calls_total"),
                func.sum(
                    case((Call.status == "completed", 1), else_=0)
                ).label("calls_completed"),
                func.sum(
                    case((Call.outcome.isnot(None), 1), else_=0)
                ).label("calls_with_intent"),
                func.coalesce(
                    func.avg(
                        case(
                            (Call.status == "completed", Call.duration_seconds),
                            else_=None,
                        )
                    ),
                    0,
                ).label("avg_duration"),
            )
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= window_start)
            .where(Call.started_at <= window_end)
        )
    ).one()

    calls_total = int(call_agg.calls_total or 0)
    calls_completed = int(call_agg.calls_completed or 0)
    calls_with_intent = int(call_agg.calls_with_intent or 0)
    avg_duration = int(round(float(call_agg.avg_duration or 0)))

    # ── Bookings ─────────────────────────────────────────────────────────────
    bookings_created = (
        await db.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.practice_id == practice.id)
            .where(Booking.created_at >= window_start)
            .where(Booking.created_at <= window_end)
        )
    ).scalar_one()

    # ── Top procedures ────────────────────────────────────────────────────────
    procedure_rows = (
        await db.execute(
            select(
                Booking.procedure_type.label("procedure"),
                func.count().label("count"),
            )
            .where(Booking.practice_id == practice.id)
            .where(Booking.created_at >= window_start)
            .where(Booking.created_at <= window_end)
            .where(Booking.procedure_type.isnot(None))
            .group_by(Booking.procedure_type)
            .order_by(func.count().desc())
            .limit(5)
        )
    ).all()

    top_procedures = [
        ProcedureCount(procedure=row.procedure, count=int(row.count))  # type: ignore[call-overload]
        for row in procedure_rows
    ]

    conversion_rate = round(bookings_created / calls_total, 3) if calls_total else 0.0
    ai_answer_rate = round(calls_completed / calls_total, 3) if calls_total else 0.0

    return ConversionResponse(
        period_days=period_days,
        calls_total=calls_total,
        calls_completed=calls_completed,
        calls_with_booking_intent=calls_with_intent,
        bookings_created=int(bookings_created),
        conversion_rate=conversion_rate,
        ai_answer_rate=ai_answer_rate,
        avg_call_duration_seconds=avg_duration,
        top_procedures=top_procedures,
    )


@router.get("/roi", response_model=ROIResponse)
async def dashboard_roi(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> ROIResponse:
    """Return ROI metrics for the last 30 days."""
    period_days = 30
    today = datetime.now(UTC).date()
    window_start = datetime.combine(today - timedelta(days=period_days - 1), time.min, tzinfo=UTC)
    window_end = datetime.combine(today, time.max, tzinfo=UTC)

    # ── Calls handled by AI (all completed / non-in_progress calls) ──────────
    calls_handled_by_ai = (
        await db.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= window_start)
            .where(Call.started_at <= window_end)
            .where(Call.status != "in_progress")
        )
    ).scalar_one()

    # ── Missed calls ─────────────────────────────────────────────────────────
    calls_missed = (
        await db.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= window_start)
            .where(Call.started_at <= window_end)
            .where(Call.status == "missed")
        )
    ).scalar_one()

    # ── Bookings created by AI call ───────────────────────────────────────────
    bookings_by_ai = (
        await db.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.practice_id == practice.id)
            .where(Booking.created_at >= window_start)
            .where(Booking.created_at <= window_end)
            .where(Booking.source == "ai_call")
        )
    ).scalar_one()

    # ── Talk time aggregates from completed calls ─────────────────────────────
    talk_agg = (
        await db.execute(
            select(
                func.coalesce(func.sum(Call.duration_seconds), 0).label("total_duration"),
            )
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= window_start)
            .where(Call.started_at <= window_end)
            .where(Call.status == "completed")
        )
    ).one()

    total_talk_time_minutes = int(round(float(talk_agg.total_duration or 0) / 60))

    # ── Derived metrics ───────────────────────────────────────────────────────
    minutes_saved = total_talk_time_minutes
    cost_saved_usd = round(minutes_saved / 60 * 25, 2)
    revenue_protected_usd = round(int(bookings_by_ai) * 150, 2)
    ai_answer_rate_pct = round(
        int(calls_handled_by_ai) / max(int(calls_handled_by_ai) + int(calls_missed), 1) * 100,
        1,
    )

    return ROIResponse(
        period_days=period_days,
        calls_handled_by_ai=int(calls_handled_by_ai),
        calls_missed=int(calls_missed),
        bookings_by_ai=int(bookings_by_ai),
        total_talk_time_minutes=total_talk_time_minutes,
        minutes_saved=minutes_saved,
        cost_saved_usd=cost_saved_usd,
        revenue_protected_usd=revenue_protected_usd,
        ai_answer_rate_pct=ai_answer_rate_pct,
    )
