"""Appointment-reminder scheduler.

Texts patients a reminder ~24h and ~2h before their appointment. Runs two
ways, mirroring call_sync:
  * one-shot via :func:`send_due_reminders` (used by tests/scripts), and
  * as a periodic background loop via :func:`reminder_loop`, started from the
    FastAPI lifespan when ``REMINDERS_ENABLED=true``.

Idempotency: each booking carries ``reminder_24h_sent_at`` / ``reminder_2h_sent_at``.
A reminder is only sent when its column is NULL and the appointment falls in the
matching time window, then the column is stamped — so the loop never double-sends.

Fail-safe: SMS errors never raise (send_sms swallows them); a bad row can't stop
the rest of the batch.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

import app.db as app_db
from app.config import get_settings
from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.practice import Practice
from app.services.sms import send_appointment_reminder

logger = logging.getLogger(__name__)


async def _send_window(
    session,
    *,
    practice_id,
    now: datetime,
    lower: datetime,
    upper: datetime,
    column: str,
    soon: bool,
    client=None,
) -> int:
    """Send reminders for confirmed bookings whose appointment is in
    (lower, upper] and whose ``column`` reminder hasn't been sent yet.

    Scoped to a single practice; the session's tenant must already be set
    (the ``patients`` join is RLS-protected).

    Returns the number of reminders sent.
    """
    sent_col = getattr(Booking, column)
    rows = (
        await session.execute(
            select(Booking, Patient, Practice)
            .join(Patient, Booking.patient_id == Patient.id)
            .join(Practice, Booking.practice_id == Practice.id)
            .where(Booking.practice_id == practice_id)
            .where(Booking.status == "confirmed")
            .where(sent_col.is_(None))
            .where(Booking.appointment_at > lower)
            .where(Booking.appointment_at <= upper)
        )
    ).all()

    count = 0
    for booking, patient, practice in rows:
        await send_appointment_reminder(
            to=patient.phone,
            practice_name=practice.name,
            first_name=patient.first_name,
            date=booking.appointment_at.date().isoformat(),
            time=booking.appointment_at.strftime("%H:%M"),
            soon=soon,
            opted_out=patient.sms_opt_out,
            client=client,
        )
        # Stamp regardless of SMS result — a disabled/failed SMS must not cause an
        # infinite retry loop. The send itself is fail-safe and logged.
        setattr(booking, column, now)
        count += 1
    return count


def _within_quiet_window(
    now: datetime, tz_name: str | None, start_hour: int, end_hour: int
) -> bool:
    """True when the practice-local hour is within [start_hour, end_hour).

    Outside this window we hold reminders (quiet hours). Unknown timezone falls
    back to UTC so we still send rather than silently never sending.
    """
    try:
        tz = ZoneInfo(tz_name) if tz_name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    local_hour = now.astimezone(tz).hour
    return start_hour <= local_hour < end_hour


async def send_due_reminders(*, now: datetime | None = None, client=None) -> dict:
    """Send all due 24h and 2h reminders across every practice.

    Iterates practices and sets the tenant per practice so the RLS-protected
    ``patients`` join works whether the DB connection is a superuser (prod) or
    the RLS-enforced app role (tests). Returns a small summary dict.
    """
    now = now or datetime.now(tz=UTC)
    settings = get_settings()
    quiet_start = settings.reminder_quiet_start_hour
    quiet_end = settings.reminder_quiet_end_hour
    sent_24h = sent_2h = 0

    async with app_db.async_session_factory() as session:
        # Only practices that have opted in (per-practice toggle; the global
        # REMINDERS_ENABLED env already gated whether this loop runs at all).
        practices = (
            await session.execute(
                select(Practice.id, Practice.timezone)
                .where(Practice.reminders_enabled.is_(True))
                .order_by(Practice.created_at)
            )
        ).all()

        for practice_id, tz_name in practices:
            # Quiet hours: skip practices where it's currently too early/late
            # in their LOCAL time (TCPA-friendly — no 3am texts).
            if not _within_quiet_window(now, tz_name, quiet_start, quiet_end):
                continue
            await set_tenant(session, practice_id)
            # 24h pass: appointment is between 2h and 24h away (so it never
            # collides with the 2h reminder for the same booking).
            sent_24h += await _send_window(
                session,
                practice_id=practice_id,
                now=now,
                lower=now + timedelta(hours=2),
                upper=now + timedelta(hours=24),
                column="reminder_24h_sent_at",
                soon=False,
                client=client,
            )
            # 2h pass: appointment is within the next 2h.
            sent_2h += await _send_window(
                session,
                practice_id=practice_id,
                now=now,
                lower=now,
                upper=now + timedelta(hours=2),
                column="reminder_2h_sent_at",
                soon=True,
                client=client,
            )
        await session.commit()

    summary = {"sent_24h": sent_24h, "sent_2h": sent_2h}
    return summary


async def reminder_loop() -> None:
    """Run :func:`send_due_reminders` forever on a fixed interval."""
    interval = get_settings().reminder_interval_seconds
    logger.info("reminder loop started (every %ss)", interval)
    while True:
        try:
            result = await send_due_reminders()
            if result.get("sent_24h") or result.get("sent_2h"):
                logger.info("reminders: %s", result)
        except asyncio.CancelledError:
            logger.info("reminder loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("reminder iteration failed; will retry next tick")
        await asyncio.sleep(interval)
