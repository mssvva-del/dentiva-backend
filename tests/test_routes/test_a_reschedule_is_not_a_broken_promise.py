"""A moved appointment is a promise kept.

The backstop that pages the clinic when the agent says "you're all set" and no
appointment exists looked only for a booking THIS call created. A reschedule
touches a booking an earlier call made, so every live reschedule ended with an
urgent callback to ring back a patient who had just been moved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import set_tenant
from app.models.booking import Booking
from app.models.callback_request import CallbackRequest
from app.models.patient import Patient
from app.observability import alerts
from tests.conftest import seed_practice

PHONE = "+15551238000"
ALL_SET = [
    {"role": "user", "content": "Can you move my cleaning to next week?"},
    {"role": "agent", "content": "Done. You're all set for Tuesday at ten."},
]


async def _clinic(db_session, tag: str):
    practice, _ = await seed_practice(
        db_session, name=f"Moved {tag}", clerk_org_id=f"org_m{tag}", clerk_user_id=f"user_m{tag}",
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=f"m-{tag}",
        first_name="Elena", last_name="Vasquez",
        phone=PHONE, date_of_birth="1991-06-14",
    )
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=4),
        duration_minutes=45, procedure_type="cleaning", status="confirmed",
    )
    db_session.add(booking)
    await db_session.commit()
    return practice, booking


async def _call(client, practice, call_id: str, *, started: datetime):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": PHONE, "to_number": "+16204559562",
                 "start_timestamp": int(started.timestamp() * 1000),
                 "metadata": {"practice_id": str(practice.id)}},
    })


async def _hang_up(client, practice, call_id: str):
    r = await client.post("/webhooks/retell", json={
        "event": "call_ended", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": PHONE, "to_number": "+16204559562",
                 "disconnection_reason": "user_hangup", "transcript_object": ALL_SET,
                 "metadata": {"practice_id": str(practice.id)}},
    })
    assert r.status_code == 200, r.text


async def _db_now(db_session) -> datetime:
    # updated_at is stamped by the database clock; the call's start comes from
    # the payload. Anchor both to the database so a skewed local clock cannot
    # decide the test.
    return (await db_session.execute(select(func.now()))).scalar_one()


async def test_a_reschedule_does_not_page_the_clinic(client, db_session):
    practice, booking = await _clinic(db_session, "a")
    alerts._RECENT.clear()
    started = await _db_now(db_session)  # after the booking exists, before it moves
    await _call(client, practice, "moved-a", started=started)

    # What reschedule_appointment does: the same booking, a new time.
    booking.appointment_at = booking.appointment_at + timedelta(days=7)
    await db_session.commit()

    await _hang_up(client, practice, "moved-a")

    assert "booking_promised_not_made" not in alerts.recent_alerts()["by_kind"]
    await set_tenant(db_session, practice.id)
    pending = (await db_session.execute(
        select(CallbackRequest.id).where(CallbackRequest.practice_id == practice.id)
    )).all()
    assert pending == []


async def test_a_promise_with_nothing_behind_it_still_pages(client, db_session):
    """The case the backstop exists for must keep firing: same caller, same
    words, but no appointment of theirs changed during the call."""
    practice, _ = await _clinic(db_session, "b")
    alerts._RECENT.clear()
    started = await _db_now(db_session)  # the booking predates the call
    await _call(client, practice, "moved-b", started=started)
    await _hang_up(client, practice, "moved-b")

    assert "booking_promised_not_made" in alerts.recent_alerts()["by_kind"]
