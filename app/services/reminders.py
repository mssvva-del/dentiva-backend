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
from app.services.availability import slot_from_utc
from app.services.sms import send_appointment_reminder
from app.services.worker_lock import advisory_tick_lock

logger = logging.getLogger(__name__)


async def _claim_window(
    session,
    *,
    practice_id,
    now: datetime,
    lower: datetime,
    upper: datetime,
    column: str,
    soon: bool,
) -> list[dict]:
    """CLAIM (not send) reminders for confirmed bookings whose appointment is in
    (lower, upper] and whose ``column`` reminder hasn't been stamped yet.

    Claim-first: we stamp the idempotency column here and let the caller COMMIT
    before any SMS leaves. Sending happens strictly AFTER that commit, so the
    failure mode is at-most-once — a crash between commit and send DROPS a
    reminder (a patient simply doesn't get a text), it never sends a DUPLICATE.
    Sending-then-stamping had the opposite, worse failure: SMS out, crash before
    commit, retry re-sends. For appointment reminders a missed text is far less
    harmful than a duplicate one.

    Scoped to a single practice; the session's tenant must already be set (the
    ``patients`` join is RLS-protected). SKIP LOCKED lets a second worker claim a
    disjoint batch instead of blocking. Returns the send payloads (plain dicts,
    detached from the session so they survive the commit).
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
            .with_for_update(skip_locked=True, of=Booking)
        )
    ).all()

    payloads: list[dict] = []
    for booking, patient, practice in rows:
        # The clinic's own clock. appointment_at is UTC, so the raw column put an
        # hour in the reminder that did not match the confirmation text the same
        # patient got when they booked — off by the offset, and on a late Pacific
        # appointment naming the wrong day.
        local_date, local_time = slot_from_utc(booking.appointment_at, practice.timezone)
        payloads.append(
            {
                "to": patient.phone,
                "practice_name": practice.name,
                "first_name": patient.first_name,
                "date": local_date,
                "time": local_time,
                "soon": soon,
                "opted_out": patient.sms_opt_out,
            }
        )
        # Stamp inside the claim transaction. The stamp is durable BEFORE the send,
        # and stays stamped even if the later send is disabled/fails — a failed SMS
        # must not cause an infinite retry loop (send is fail-safe and logged).
        setattr(booking, column, now)
    return payloads


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

    # Only practices that have opted in (per-practice toggle; the global
    # REMINDERS_ENABLED env already gated whether this loop runs at all).
    async with app_db.async_session_factory() as session:
        practices = (
            await session.execute(
                select(Practice.id, Practice.timezone)
                .where(Practice.reminders_enabled.is_(True))
                .order_by(Practice.created_at)
            )
        ).all()

    sent_24h = sent_2h = 0
    for practice_id, tz_name in practices:
        # Quiet hours: skip practices where it's currently too early/late in
        # their LOCAL time (TCPA-friendly — no 3am texts).
        if not _within_quiet_window(now, tz_name, quiet_start, quiet_end):
            continue

        # ONE session per practice. set_tenant sets a CONNECTION-scoped GUC and
        # RLS is FORCE'd, so reusing a single session across practices would flush
        # one practice's stamps under another's tenant (RLS then filters the
        # UPDATE to 0 rows). A fresh session per practice keeps each tenant clean —
        # same pattern as call_sync / scrub_expired_transcripts.
        async with app_db.async_session_factory() as session:
            await set_tenant(session, practice_id)
            # Phase 1 — CLAIM: stamp due reminders, COMMIT before any SMS goes
            # out. Payloads come back as plain dicts that survive the commit.
            # 24h pass: appointment is between 2h and 24h away (so it never
            # collides with the 2h reminder for the same booking).
            claimed_24h = await _claim_window(
                session,
                practice_id=practice_id,
                now=now,
                lower=now + timedelta(hours=2),
                upper=now + timedelta(hours=24),
                column="reminder_24h_sent_at",
                soon=False,
            )
            # 2h pass: appointment is within the next 2h.
            claimed_2h = await _claim_window(
                session,
                practice_id=practice_id,
                now=now,
                lower=now,
                upper=now + timedelta(hours=2),
                column="reminder_2h_sent_at",
                soon=True,
            )
            await session.commit()

        # Phase 2 — SEND: only after this practice's claim is durable (session
        # closed). send_sms is fail-safe; we also isolate each send so one raising
        # call can't skip the rest. A crash here only DROPS a reminder, never
        # dupes one — the stamp is already committed.
        for payload in (*claimed_24h, *claimed_2h):
            try:
                await send_appointment_reminder(**payload, client=client)
            except Exception:  # noqa: BLE001 — a bad send must not abort the batch
                logger.exception("reminder send failed after claim (dropped, not retried)")

        # "sent" counts the CLAIMED reminders — the stamp is what makes a reminder
        # done (send is best-effort and never retried), so the count is stable.
        sent_24h += len(claimed_24h)
        sent_2h += len(claimed_2h)

    summary = {"sent_24h": sent_24h, "sent_2h": sent_2h}
    return summary


async def reminder_loop() -> None:
    """Run :func:`send_due_reminders` forever on a fixed interval."""
    interval = get_settings().reminder_interval_seconds
    logger.info("reminder loop started (every %ss)", interval)
    while True:
        try:
            # Single-leader: only the lock winner sends this tick (no double texts
            # across instances). Claim-first inside makes it at-most-once anyway,
            # but the lock avoids two instances racing the same rows every tick.
            async with advisory_tick_lock("reminders") as leader:
                if leader:
                    result = await send_due_reminders()
                    if result.get("sent_24h") or result.get("sent_2h"):
                        logger.info("reminders: %s", result)
        except asyncio.CancelledError:
            logger.info("reminder loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("reminder iteration failed; will retry next tick")
        await asyncio.sleep(interval)
