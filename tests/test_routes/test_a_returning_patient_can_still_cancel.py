"""The birthday we never wrote down.

A caller who says they have been to the office before is never asked for a date
of birth — only new patients are, because that is what opens a chart in the
practice's software. So our copy of a returning patient has no birthday at all.

Ring back to cancel and the agent asks for one. The patient recites it
correctly, it matches nothing, and they are told there is no appointment under
their number. The chair stays blocked and they leave the call believing they are
still booked. Seen on a live call, on a real appointment we had just made.

For the tools that only act on an appointment that already exists, one person on
this number is answer enough — caller ID ties them to it and the identity check
still runs. For BOOKING it must stay strict: the same fallback would file a
child's visit in a parent's chart.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from tests.conftest import seed_practice

PHONE = "+15551239000"


async def _clinic(db_session, tag: str, *, dob: str | None = None):
    practice, _ = await seed_practice(
        db_session, name=f"Returning {tag}",
        clerk_org_id=f"org_r{tag}", clerk_user_id=f"user_r{tag}",
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=f"r-{tag}",
        first_name="Ruth", last_name="Delaney", phone=PHONE, date_of_birth=dob,
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
    return practice, patient, booking


async def _call_from(client, practice, phone: str, call_id: str):
    """A call already in progress, so the tool resolves the tenant and caller."""
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": phone,
                 "to_number": practice.phone_number or "+16204559562",
                 "metadata": {"practice_id": str(practice.id)}},
    })


async def test_the_date_of_birth_we_never_recorded_does_not_lose_the_appointment(
    client, db_session
):
    practice, _, booking = await _clinic(db_session, "a")  # no DOB on file
    await _call_from(client, practice, PHONE, "ret-a")

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "ret-a",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": PHONE, "patient_dob": "1968-04-09"},
    })
    assert r.status_code == 200, r.text
    assert r.json().get("cancelled") is True, r.json()

    # The handler committed on its own session; ids are read first because an
    # expired instance cannot lazy-load here.
    pid, bid = practice.id, booking.id
    db_session.expire_all()
    await set_tenant(db_session, pid)
    fresh = (await db_session.execute(
        select(Booking).where(Booking.id == bid)
    )).scalar_one()
    assert fresh.status == "cancelled"


async def test_a_household_line_still_has_to_say_which_of_them_it_is(
    client, db_session
):
    """Two people on one number and a birthday matching neither: the fallback
    must not fire. Cancelling the wrong person's appointment is the failure this
    lookup exists to prevent."""
    practice, _, booking = await _clinic(db_session, "b")
    await set_tenant(db_session, practice.id)
    db_session.add(Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="r-b2",
        first_name="Tom", last_name="Delaney", phone=PHONE,
    ))
    await db_session.commit()

    await _call_from(client, practice, PHONE, "ret-b")
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "ret-b",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": PHONE, "patient_dob": "1968-04-09"},
    })
    assert r.json().get("cancelled") is False

    # The handler committed on its own session; ids are read first because an
    # expired instance cannot lazy-load here.
    pid, bid = practice.id, booking.id
    db_session.expire_all()
    await set_tenant(db_session, pid)
    fresh = (await db_session.execute(
        select(Booking).where(Booking.id == bid)
    )).scalar_one()
    assert fresh.status == "confirmed", "somebody else's appointment was cancelled"


async def test_a_wrong_birthday_against_a_chart_that_has_one_is_still_refused(
    client, db_session
):
    """The fallback is for a birthday we never wrote down, not for one that
    disagrees with what we did."""
    practice, _, booking = await _clinic(db_session, "c", dob="1971-02-02")
    await _call_from(client, practice, PHONE, "ret-c")

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "ret-c",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": PHONE, "patient_dob": "1968-04-09"},
    })
    assert r.json().get("cancelled") is False

    # The handler committed on its own session; ids are read first because an
    # expired instance cannot lazy-load here.
    pid, bid = practice.id, booking.id
    db_session.expire_all()
    await set_tenant(db_session, pid)
    fresh = (await db_session.execute(
        select(Booking).where(Booking.id == bid)
    )).scalar_one()
    assert fresh.status == "confirmed"


async def test_booking_never_takes_the_shortcut(client, db_session):
    """A child booking from the family phone, giving their own birthday, must
    get their own record — not the parent's chart the number is registered to."""
    practice, parent, _ = await _clinic(db_session, "d")
    await _call_from(client, practice, PHONE, "ret-d")

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "ret-d",
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Ellie", "patient_last_name": "Delaney",
                 "patient_phone": PHONE, "patient_dob": "2011-07-19",
                 "procedure": "cleaning"},
    })
    assert r.status_code == 200, r.text

    await set_tenant(db_session, practice.id)
    people = (await db_session.execute(
        select(Patient).where(Patient.practice_id == practice.id)
    )).scalars().all()
    assert len(people) == 2, "the child's booking landed in the parent's chart"
    assert {(p.first_name or "") for p in people} == {"Ruth", "Ellie"}
    assert str(parent.id) in {str(p.id) for p in people}
