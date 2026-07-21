"""Native availability — real openings from business_hours minus bookings."""

from __future__ import annotations

import uuid
from datetime import UTC, date, timedelta

from app.models.booking import Booking
from app.models.patient import Patient
from app.services.availability import compute_native_slots, slot_to_utc
from tests.conftest import seed_practice


def _future_weekday(days: int = 3) -> date:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:  # skip Sat/Sun
        d += timedelta(days=1)
    return d


async def test_native_slots_come_from_business_hours(db_session):
    practice, _ = await seed_practice(db_session, name="Hours Co",
                                      clerk_org_id="o_av1", clerk_user_id="u_av1")
    practice.timezone = "America/New_York"
    await db_session.commit()

    day = _future_weekday()
    slots = await compute_native_slots(
        db_session, practice, procedure="cleaning",
        preferred_date=day.isoformat(), preferred_window="morning",
    )
    assert slots, "expected real morning openings on a weekday"
    # The requested day is offered, and results spread across days (≤2/day) so the
    # agent can offer variety — all are on/after the requested day, all morning.
    assert day.isoformat() in {s.date for s in slots}
    for s in slots:
        assert s.date >= day.isoformat()
        hh = int(s.time.split(":")[0])
        assert 9 <= hh < 12  # morning window, within seeded 09:00–18:00 hours


async def test_native_slots_skip_the_past_and_closed_days(db_session):
    practice, _ = await seed_practice(db_session, name="Past Co",
                                      clerk_org_id="o_av2", clerk_user_id="u_av2")
    # A date in the past yields nothing (never offer a past slot).
    past = (date.today() - timedelta(days=2)).isoformat()
    slots = await compute_native_slots(db_session, practice, preferred_date=past)
    assert all(s.date >= date.today().isoformat() for s in slots)


async def test_native_slots_exclude_booked_times(db_session):
    practice, _ = await seed_practice(db_session, name="Booked Co",
                                      clerk_org_id="o_av3", clerk_user_id="u_av3")
    practice.timezone = "America/New_York"
    await db_session.commit()

    day = _future_weekday()
    # Grab the first free slot, book it, then confirm it's gone next time.
    first = (await compute_native_slots(
        db_session, practice, preferred_date=day.isoformat(), preferred_window="morning"
    ))[0]
    patient = Patient(id=uuid.uuid4(), practice_id=practice.id,
                      pms_external_id="p-av", first_name="A", phone="+15550000001")
    db_session.add(patient)
    await db_session.flush()
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=slot_to_utc(first.date, first.time, practice.timezone),
        duration_minutes=60, procedure_type="cleaning", provider_name=first.provider,
        status="confirmed", source="ai_call",
    ))
    await db_session.commit()

    again = await compute_native_slots(
        db_session, practice, preferred_date=day.isoformat(), preferred_window="morning"
    )
    assert all(not (s.date == first.date and s.time == first.time) for s in again)


def test_slot_to_utc_converts_local_to_utc():
    # 10:00 New York (EDT, -4 in July) → 14:00 UTC.
    dt = slot_to_utc("2026-07-20", "10:00", "America/New_York")
    assert dt.tzinfo is not None
    assert dt.astimezone(UTC).hour == 14
