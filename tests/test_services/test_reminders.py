"""Tests for the appointment-reminder scheduler.

SMS is disabled in tests, so send_appointment_reminder returns
{"skipped": "sms_disabled"} (no network). We assert the scheduler picks the
right bookings for each window, stamps the idempotency columns, and never
double-sends on a second pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.services import reminders
from app.services.sms import build_reminder_body
from tests.conftest import seed_practice


def test_build_reminder_body_wording():
    soon = build_reminder_body(
        practice_name="Smile", first_name="Maria", date="2099-12-15", time="10:00", soon=True
    )
    later = build_reminder_body(
        practice_name="Smile", first_name="Maria", date="2099-12-15", time="10:00", soon=False
    )
    assert "later today" in soon
    assert "tomorrow" in later
    assert "Smile" in soon and "Maria" in soon


async def _add_booking(session, practice_id, *, when, ext, phone="+15557770000"):
    await set_tenant(session, practice_id)
    patient = Patient(
        practice_id=practice_id,
        pms_external_id=ext,
        first_name="Maria",
        phone=phone,
    )
    session.add(patient)
    await session.flush()
    booking = Booking(
        practice_id=practice_id,
        patient_id=patient.id,
        appointment_at=when,
        procedure_type="cleaning",
        provider_name="Dr. Smith",
    )
    session.add(booking)
    await session.flush()
    return booking


async def test_reminders_pick_correct_windows_and_are_idempotent(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Reminder Dental", clerk_org_id="org_rem1", clerk_user_id="user_rem1"
    )
    # Pin tz + a midday instant so quiet-hours never skips this window test.
    practice.timezone = "UTC"
    now = datetime(2099, 1, 15, 12, 0, tzinfo=UTC)

    b_24h = await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=23), ext="rem-24h"
    )
    b_2h = await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=1), ext="rem-2h"
    )
    b_far = await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=48), ext="rem-far"
    )
    await db_session.commit()

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 1, "sent_2h": 1}

    # The scheduler committed in its own session; re-query with populate_existing
    # so our identity-mapped objects pick up the freshly stamped columns.
    await set_tenant(db_session, practice.id)
    rows = {
        b.id: b
        for b in (
            await db_session.execute(
                select(Booking)
                .where(Booking.practice_id == practice.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert rows[b_24h.id].reminder_24h_sent_at is not None
    assert rows[b_24h.id].reminder_2h_sent_at is None
    assert rows[b_2h.id].reminder_2h_sent_at is not None
    assert rows[b_2h.id].reminder_24h_sent_at is None  # too close for the 24h window
    assert rows[b_far.id].reminder_24h_sent_at is None
    assert rows[b_far.id].reminder_2h_sent_at is None

    # Second pass at the same instant sends nothing (idempotent).
    summary2 = await reminders.send_due_reminders(now=now)
    assert summary2 == {"sent_24h": 0, "sent_2h": 0}


async def test_reminders_skip_cancelled(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Reminder Dental 2", clerk_org_id="org_rem2", clerk_user_id="user_rem2"
    )
    now = datetime.now(tz=UTC)
    b = await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=10), ext="rem-cancel"
    )
    b.status = "cancelled"
    await db_session.commit()

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 0, "sent_2h": 0}

    await set_tenant(db_session, practice.id)
    refreshed = (
        await db_session.execute(
            select(Booking)
            .where(Booking.id == b.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.reminder_24h_sent_at is None


async def test_reminders_skip_practice_opted_out(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Opted Out Dental", clerk_org_id="org_rem_off",
        clerk_user_id="user_rem_off",
    )
    # Practice opts out of reminders.
    practice.reminders_enabled = False
    now = datetime.now(tz=UTC)
    await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=10), ext="rem-off"
    )
    await db_session.commit()

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 0, "sent_2h": 0}


async def test_reminders_quiet_hours_skip(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Quiet Dental", clerk_org_id="org_quiet",
        clerk_user_id="user_quiet",
    )
    practice.timezone = "UTC"  # make local == UTC for a deterministic hour
    # 03:00 local — before the 08:00 quiet-start → must hold all reminders.
    now = datetime(2099, 1, 15, 3, 0, tzinfo=UTC)
    await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=10), ext="quiet-1"
    )
    await db_session.commit()

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 0, "sent_2h": 0}


async def test_reminders_multi_practice_each_tenant_stamped(client, db_session):
    """Two practices with due bookings in ONE tick — each must get stamped. This
    exercises the session-per-practice path: reusing one session across tenants
    under RLS FORCE would flush one clinic's stamp under the other's tenant and
    silently drop it. Both bookings stamped + counted proves the isolation."""
    now = datetime(2099, 1, 15, 12, 0, tzinfo=UTC)
    p1, _ = await seed_practice(
        db_session, name="Multi A", clerk_org_id="org_ma", clerk_user_id="user_ma"
    )
    p2, _ = await seed_practice(
        db_session, name="Multi B", clerk_org_id="org_mb", clerk_user_id="user_mb"
    )
    p1.timezone = "UTC"
    p2.timezone = "UTC"
    b1 = await _add_booking(db_session, p1.id, when=now + timedelta(hours=23),
                            ext="ma-24h", phone="+15550000001")
    b2 = await _add_booking(db_session, p2.id, when=now + timedelta(hours=23),
                            ext="mb-24h", phone="+15550000002")
    await db_session.commit()

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 2, "sent_2h": 0}  # BOTH clinics stamped

    for pid, bid in ((p1.id, b1.id), (p2.id, b2.id)):
        await set_tenant(db_session, pid)
        row = (
            await db_session.execute(
                select(Booking).where(Booking.id == bid)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.reminder_24h_sent_at is not None


async def test_reminders_claim_first_survives_send_failure(client, db_session, monkeypatch):
    """Claim-first invariant: the reminder is STAMPED and committed BEFORE the SMS
    goes out. So even if every send crashes, (a) the reminder is marked done — no
    infinite retry — and (b) a second pass sends nothing (no duplicate). This is
    the at-most-once guarantee that replaces the old send-then-commit dup window."""
    practice, _ = await seed_practice(
        db_session, name="ClaimFirst Dental", clerk_org_id="org_cf",
        clerk_user_id="user_cf",
    )
    practice.timezone = "UTC"
    now = datetime(2099, 1, 15, 12, 0, tzinfo=UTC)
    b = await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=23), ext="cf-24h"
    )
    await db_session.commit()

    async def _boom(**_kw):
        raise RuntimeError("sms provider down")

    # send raises for every call — the batch must still complete and stay claimed.
    monkeypatch.setattr(reminders, "send_appointment_reminder", _boom)

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 1, "sent_2h": 0}  # claimed despite the crash

    await set_tenant(db_session, practice.id)
    refreshed = (
        await db_session.execute(
            select(Booking).where(Booking.id == b.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.reminder_24h_sent_at is not None  # stamp is durable

    # Second pass: nothing re-sent — no retry storm, no duplicate.
    summary2 = await reminders.send_due_reminders(now=now)
    assert summary2 == {"sent_24h": 0, "sent_2h": 0}


async def test_reminders_send_during_open_hours(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Open Hours Dental", clerk_org_id="org_open",
        clerk_user_id="user_open",
    )
    practice.timezone = "UTC"
    # 12:00 local — within 08:00–21:00 → reminders flow normally.
    now = datetime(2099, 1, 15, 12, 0, tzinfo=UTC)
    await _add_booking(
        db_session, practice.id, when=now + timedelta(hours=10), ext="open-1"
    )
    await db_session.commit()

    summary = await reminders.send_due_reminders(now=now)
    assert summary == {"sent_24h": 1, "sent_2h": 0}
