from __future__ import annotations

from datetime import UTC, datetime, time

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.call import Call
from app.models.practice import Practice
from app.schemas.booking import BriefingResponse, DashboardToday

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
    peak_rows = (
        await db.execute(
            select(
                func.extract("hour", Call.started_at).label("hour"),
                func.count().label("count"),
            )
            .where(Call.practice_id == practice.id)
            .where(Call.started_at >= day_start)
            .where(Call.started_at <= day_end)
            .group_by(func.extract("hour", Call.started_at))
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
