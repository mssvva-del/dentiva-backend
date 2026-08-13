"""A redelivered reschedule must not move the appointment a second time.

Retell redelivers a function_call when its own request times out — a network
hiccup on their side, not a second request from the patient. book_appointment
has guarded against that since the beginning; reschedule never did.

On redelivery this handler found the appointment it had ALREADY moved, saw the
new slot as taken (taken by this very booking), and moved it again to the next
free time. The patient agreed to one time, got a second text naming another, and
would arrive at a clinic expecting neither. With a PMS connected it moves in the
practice's real calendar twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.booking import Booking
from app.models.patient import Patient
from tests.conftest import seed_practice

_PHONE = "+16205551234"


async def _patient_with_booking(db_session, practice_id):
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice_id, first_name="Ann", last_name="Lee",
        phone=_PHONE, pms_external_id="P-1",
    )
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice_id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=3),
        status="confirmed", procedure_type="cleaning", provider_name="Dr. Smith",
    )
    db_session.add(booking)
    await db_session.commit()
    return booking.id


async def _reschedule(client, call_id, new_date):
    return await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": "reschedule_appointment",
        "args": {"patient_phone": _PHONE, "new_date": new_date},
    })


async def test_a_redelivered_reschedule_leaves_the_time_alone(client, db_session):
    """THE test. Same call_id twice — the appointment must land in one place."""
    practice, _ = await seed_practice(
        db_session, name="Move Dental", clerk_org_id="org_mv1", clerk_user_id="u_mv1"
    )
    booking_id = await _patient_with_booking(db_session, practice.id)

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "resched-1",
        "call": {"from_number": _PHONE, "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })

    target = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    first = (await _reschedule(client, "resched-1", target)).json()
    assert first["rescheduled"] is True, first

    db_session.expire_all()
    after_first = (await db_session.execute(
        select(Booking).where(Booking.id == booking_id)
    )).scalar_one().appointment_at

    second = (await _reschedule(client, "resched-1", target)).json()
    assert second["rescheduled"] is True, second

    db_session.expire_all()
    after_second = (await db_session.execute(
        select(Booking).where(Booking.id == booking_id)
    )).scalar_one().appointment_at

    assert after_first == after_second, (
        "a redelivered reschedule moved the appointment a second time"
    )
    # And it says the same time as the first answer, so the agent has nothing
    # new to read out and the patient hears one story.
    assert second["appointment"]["time"] == first["appointment"]["time"]
    assert second["appointment"]["date"] == first["appointment"]["date"]


async def test_a_genuine_second_reschedule_still_works(client, db_session):
    """The guard is per CALL, not per booking. A patient who rings back tomorrow
    to move the same appointment again must be able to."""
    practice, _ = await seed_practice(
        db_session, name="Move Dental 2", clerk_org_id="org_mv2", clerk_user_id="u_mv2"
    )
    booking_id = await _patient_with_booking(db_session, practice.id)

    for call_id, days in (("call-a", 10), ("call-b", 12)):
        await client.post("/webhooks/retell", json={
            "event": "call_started", "call_id": call_id,
            "call": {"from_number": _PHONE, "to_number": "+15559876543",
                     "start_timestamp": 1748563200000},
        })
        target = (datetime.now(UTC) + timedelta(days=days)).date().isoformat()
        body = (await _reschedule(client, call_id, target)).json()
        assert body["rescheduled"] is True, body

    db_session.expire_all()
    final = (await db_session.execute(
        select(Booking).where(Booking.id == booking_id)
    )).scalar_one().appointment_at
    assert final.date().isoformat() >= (
        datetime.now(UTC) + timedelta(days=12)
    ).date().isoformat(), "the second call did not move it"
