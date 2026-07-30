"""Native availability — real openings from the clinic's OWN hours + bookings.

Most pilot clinics have no live PMS API wired. Until they do, the AI must still
offer REAL times, not the mock adapter's invented slots (a dentist catches a fake
"Tuesday 10am" on the first call). This computes free slots from:

  * the practice's business_hours (per-day open/close, clinic-local), minus
  * confirmed appointments already in OUR bookings table (no double-booking),

skipping past times and honoring the caller's preferred window. When the clinic
DOES connect a real PMS (pms_system='open_dental'/'nexhealth'), the webhook uses
the adapter instead — this is the default source for the no-PMS case.

All times are the clinic's LOCAL time (that's what business_hours mean and what
the agent speaks); collisions compare against bookings converted to clinic-local.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.open_dental.models import AvailableSlot
from app.models.booking import Booking
from app.models.practice import Practice

# Order of JS/DB weekday index (Mon=0 … Sun=6) → business_hours keys.
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_WINDOWS = {
    "morning": (0, 12),      # before noon
    "afternoon": (12, 17),   # noon–5pm
    "evening": (17, 24),     # 5pm+
    "any": (0, 24),
}


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else ZoneInfo("America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/New_York")


def slot_to_utc(date_str: str, time_str: str, tz_name: str | None) -> datetime:
    """Convert a clinic-LOCAL slot (date + 'HH:MM') to a UTC datetime for storage.

    Slots are spoken and offered in the clinic's local time; the DB stores
    appointment_at as tz-aware UTC. Doing this correctly is what makes reminders
    and the dashboard show the RIGHT wall-clock time to the clinic."""
    naive = datetime.fromisoformat(f"{date_str}T{time_str}:00")
    return naive.replace(tzinfo=_tz(tz_name)).astimezone(UTC)


def _parse_hhmm(v: str) -> tuple[int, int] | None:
    try:
        h, m = v.split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def _provider_name(practice: Practice) -> str:
    """First provider the agent can name, from the KB; else a safe generic."""
    kb = practice.knowledge_base or {}
    provs = kb.get("providers") if isinstance(kb, dict) else None
    if isinstance(provs, list):
        for p in provs:
            if isinstance(p, dict) and p.get("name"):
                return str(p["name"])
    return "our team"


def _duration_minutes(practice: Practice, procedure: str) -> int:
    """Visit length from the KB appointment_types (matched loosely by name), else 60."""
    kb = practice.knowledge_base or {}
    types = kb.get("appointment_types") if isinstance(kb, dict) else None
    if isinstance(types, list) and procedure:
        want = procedure.strip().lower()
        for t in types:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").lower()
            mins = t.get("minutes")
            if name and (name in want or want in name) and isinstance(mins, int) and mins > 0:
                return min(mins, 240)
    return 60


async def _taken_intervals(
    session: AsyncSession,
    practice_id: uuid.UUID,
    tz: ZoneInfo,
    day_from: datetime,
    day_to: datetime,
) -> list[tuple[datetime, datetime]]:
    """Clinic-local [start, end) intervals of confirmed bookings in range.

    We keep the DURATION (not just the start) so a candidate slot is rejected
    whenever it OVERLAPS an existing booking — a 09:00 60-min appointment must
    block a 09:30 candidate, not only an exact 09:00 one.
    """
    rows = (
        await session.execute(
            select(Booking.appointment_at, Booking.duration_minutes)
            .where(
                Booking.practice_id == practice_id,
                Booking.status == "confirmed",
                # widen the lower bound by a visit-length so a booking that STARTS
                # before the window but overlaps into it is still counted.
                Booking.appointment_at >= (day_from - timedelta(hours=4)).astimezone(UTC),
                Booking.appointment_at < day_to.astimezone(UTC),
            )
        )
    ).all()
    intervals: list[tuple[datetime, datetime]] = []
    for dt, dur in rows:
        start = dt.astimezone(tz)
        intervals.append((start, start + timedelta(minutes=int(dur or 60))))
    return intervals


async def compute_native_slots(
    session: AsyncSession,
    practice: Practice,
    *,
    procedure: str = "cleaning",
    preferred_date: str = "",
    preferred_window: str | None = None,
    now: datetime | None = None,
    days_ahead: int = 14,
    max_slots: int = 6,
    step_minutes: int = 30,
) -> list[AvailableSlot]:
    """Real openings from business_hours − confirmed bookings. Clinic-local.

    Spreads results across days (at most 2 per day) so the agent can offer variety
    instead of six slots in one morning. Returns up to ``max_slots``.
    """
    tz = _tz(practice.timezone)
    now_local = (now or datetime.now(tz=UTC)).astimezone(tz)
    hours = practice.business_hours or {}
    duration = _duration_minutes(practice, procedure)
    provider = _provider_name(practice)
    lo, hi = _WINDOWS.get((preferred_window or "any").lower(), _WINDOWS["any"])

    # Start from the requested date if it's valid and not in the past, else today.
    start_day = now_local.date()
    pref = (preferred_date or "").strip()
    if pref:
        try:
            d = datetime.fromisoformat(pref).date()
            if d >= start_day:
                start_day = d
        except ValueError:
            pass

    window_from = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
    window_to = window_from + timedelta(days=days_ahead)
    taken = await _taken_intervals(session, practice.id, tz, window_from, window_to)

    def _overlaps(slot_start: datetime, slot_end: datetime) -> bool:
        # Standard half-open overlap: [a,b) intersects [c,d) iff a < d and c < b.
        return any(slot_start < end and start < slot_end for start, end in taken)

    # A small buffer so we never offer a slot that's basically "now".
    earliest = now_local + timedelta(minutes=30)

    slots: list[AvailableSlot] = []
    for offset in range(days_ahead):
        if len(slots) >= max_slots:
            break
        day = start_day + timedelta(days=offset)
        key = _WEEKDAY_KEYS[day.weekday()]
        h = hours.get(key)
        if not isinstance(h, dict):
            continue  # closed that day
        open_hm, close_hm = _parse_hhmm(h.get("open", "")), _parse_hhmm(h.get("close", ""))
        if not open_hm or not close_hm:
            continue
        cursor = datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(
            hour=open_hm[0], minute=open_hm[1]
        )
        close_dt = datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(
            hour=close_hm[0], minute=close_hm[1]
        )
        per_day = 0
        while cursor + timedelta(minutes=duration) <= close_dt and per_day < 2:
            hour = cursor.hour
            slot_end = cursor + timedelta(minutes=duration)
            if (
                lo <= hour < hi
                and cursor >= earliest
                and not _overlaps(cursor, slot_end)
            ):
                slots.append(
                    AvailableSlot(
                        date=day.isoformat(),
                        time=cursor.strftime("%H:%M"),
                        provider=provider,
                    )
                )
                per_day += 1
                if len(slots) >= max_slots:
                    break
            cursor += timedelta(minutes=step_minutes)
    return slots


# ---------------------------------------------------------------------------
# Is the office open RIGHT NOW? — the agent cannot promise a callback "in a few
# minutes" at 11pm, and an urgent caller must be told what actually happens next.
# ---------------------------------------------------------------------------

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _spoken_time(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12} {ampm}" if minute == 0 else f"{h12}:{minute:02d} {ampm}"


def office_status(practice: Practice, *, now: datetime | None = None) -> tuple[str, str]:
    """("open"|"closed", phrase for when the team is next reachable).

    The phrase is spoken to the caller, so it is relative and human: "in a few
    minutes" while open, "first thing tomorrow morning, from 9 AM" when closed.
    Unknown/empty business_hours → treated as OPEN with a vague phrase: assuming
    closed would tell a caller with a real problem to wait until tomorrow.
    """
    tz = _tz(practice.timezone)
    now_local = (now or datetime.now(tz=UTC)).astimezone(tz)
    hours = practice.business_hours or {}

    def _day(d) -> tuple[tuple[int, int], tuple[int, int]] | None:
        h = hours.get(_WEEKDAY_KEYS[d.weekday()])
        if not isinstance(h, dict):
            return None
        o, c = _parse_hhmm(h.get("open", "")), _parse_hhmm(h.get("close", ""))
        return (o, c) if o and c else None

    if not hours:
        return "open", "shortly"

    today = _day(now_local.date())
    if today is not None:
        (oh, om), (ch, cm) = today
        minutes_now = now_local.hour * 60 + now_local.minute
        if oh * 60 + om <= minutes_now < ch * 60 + cm:
            return "open", "in a few minutes"
        if minutes_now < oh * 60 + om:
            return "closed", f"as soon as we open this morning, at {_spoken_time(oh, om)}"

    # Closed for the day — find the next day we're open (a week is enough).
    for offset in range(1, 8):
        day = (now_local + timedelta(days=offset)).date()
        nxt = _day(day)
        if nxt is None:
            continue
        (oh, om), _ = nxt
        when = "tomorrow" if offset == 1 else f"on {_DAY_NAMES[day.weekday()]}"
        return "closed", f"first thing {when}, from {_spoken_time(oh, om)}"
    return "closed", "as soon as the office reopens"
