"""A caller offered nine fifteen, then refused it.

Live call, in Spanish. Pain and a swollen gum; the agent asked the triage
questions and then went on with an ordinary booking, because symptoms only ever
reached us in tool arguments and check_availability carried none. It read out
9:00 and 9:15, the caller chose, and the note attached to book_appointment —
"dolor y hinchazón en la encía" — tripped the emergency lock one sentence later.

The refusal worked. What the patient heard did not: the agent said "I'll reserve
9:15", then took a callback, then closed with "tomorrow at 9:15". She hung up
believing she had an appointment that does not exist.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import set_tenant
from app.models.booking import Booking
from app.models.call import Call
from tests.conftest import seed_practice

PHONE = "+15551239000"


async def _clinic(db_session, tag: str):
    practice, _ = await seed_practice(
        db_session, name=f"ER {tag}",
        clerk_org_id=f"org_e{tag}", clerk_user_id=f"user_e{tag}",
    )
    await db_session.commit()
    return practice


async def _calling(client, practice, call_id: str):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": PHONE,
                 "to_number": practice.phone_number or "+16204559562",
                 "metadata": {"practice_id": str(practice.id)}},
    })


async def _tool(client, call_id: str, name: str, args: dict):
    return await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": name, "args": args,
    })


async def test_the_complaint_reaches_us_before_any_time_is_read_out(
    client, db_session
):
    """The whole ordering fault: symptoms arrived with the booking, so the
    refusal could only ever come after a time had been promised."""
    practice = await _clinic(db_session, "a")
    await _calling(client, practice, "er-a")

    r = await _tool(client, "er-a", "check_availability", {
        "preferred_date": "2099-10-05", "preferred_time_window": "morning",
        "procedure": "emergency",
        "reason": "dolor y hinchazón en la encía",
    })
    body = r.json()
    assert body.get("blocked") is True, body
    assert not body.get("available_slots"), "times were read out to her anyway"


async def test_a_routine_visit_still_gets_its_times(client, db_session):
    """The lock must not fire on the ordinary reason somebody books a cleaning."""
    practice = await _clinic(db_session, "b")
    await _calling(client, practice, "er-b")

    r = await _tool(client, "er-b", "check_availability", {
        "preferred_date": "2099-10-05", "preferred_time_window": "morning",
        "procedure": "cleaning",
        "reason": "just due for a check-up",
    })
    assert r.json().get("blocked") is not True, r.json()


async def test_the_refusal_tells_the_agent_to_take_the_time_back(
    client, db_session
):
    """It read the old message and still closed with "tomorrow at 9:15"."""
    practice = await _clinic(db_session, "c")
    await _calling(client, practice, "er-c")
    await _tool(client, "er-c", "create_callback_request", {
        "patient_first_name": "Ana", "patient_phone": PHONE,
        "reason": "dolor y hinchazón", "urgent": True,
    })

    r = await _tool(client, "er-c", "book_appointment", {
        "patient_first_name": "Ana", "patient_phone": PHONE,
        "preferred_date": "2099-10-05", "preferred_time": "09:15",
        "procedure": "emergency",
    })
    body = r.json()
    assert body.get("blocked") is True
    # The shape an ordinary refusal has, so it cannot be narrated as a booking.
    assert body.get("booked") is False
    message = body["message"].lower()
    assert "not" in message and "time" in message

    await set_tenant(db_session, practice.id)
    bookings = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice.id)
    )).scalars().all()
    assert bookings == [], "an appointment was made during an emergency"


async def test_the_lock_still_survives_the_rest_of_the_call(client, db_session):
    """Sticky, as before: the caller cannot be talked back into a slot."""
    practice = await _clinic(db_session, "d")
    await _calling(client, practice, "er-d")
    await _tool(client, "er-d", "check_availability", {
        "preferred_date": "2099-10-05", "procedure": "emergency",
        "reason": "no para de sangrar",
    })

    again = await _tool(client, "er-d", "check_availability", {
        "preferred_date": "2099-10-06", "procedure": "cleaning",
    })
    assert again.json().get("blocked") is True

    await set_tenant(db_session, practice.id)
    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "er-d")
    )).scalar_one()
    assert call.emergency_active is True


async def test_one_patient_in_pain_pages_the_clinic_once(client, db_session):
    """The lock writes an urgent callback the moment it engages, because it
    cannot trust the agent to. When the agent then does it too — 1.15 seconds
    later, on a live call — the clinic was paged twice for one patient, and the
    second page is what teaches them to ignore both."""
    from app.models.callback_request import CallbackRequest

    practice = await _clinic(db_session, "e")
    await _calling(client, practice, "er-e")

    # The lock engages on a booking attempt and queues its own row.
    await _tool(client, "er-e", "book_appointment", {
        "patient_first_name": "Ana", "patient_phone": PHONE,
        "preferred_date": "2099-10-05", "procedure": "emergency",
        "notes": "dolor y hinchazón en la encía",
    })
    # The agent then asks for a callback, as the refusal told it to.
    await _tool(client, "er-e", "create_callback_request", {
        "patient_first_name": "Ana", "patient_phone": PHONE,
        "reason": "Dolor y hinchazón en la encía.", "urgent": True,
    })

    await set_tenant(db_session, practice.id)
    rows = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    )).scalars().all()
    assert len(rows) == 1, [r.reason for r in rows]
    # And it carries what the agent knew, not the lock's placeholder.
    assert rows[0].reason == "Dolor y hinchazón en la encía."
    assert rows[0].patient_first_name == "Ana"


async def test_a_second_unrelated_callback_is_still_its_own_row(client, db_session):
    """Merging is for the lock's placeholder, not for every later request."""
    from app.models.callback_request import CallbackRequest

    practice = await _clinic(db_session, "f")
    await _calling(client, practice, "er-f")
    await _tool(client, "er-f", "create_callback_request", {
        "patient_first_name": "Ana", "patient_phone": PHONE,
        "reason": "wants to ask about whitening", "urgent": False,
    })
    await _tool(client, "er-f", "create_callback_request", {
        "patient_first_name": "Ana", "patient_phone": PHONE,
        "reason": "also asking about a payment plan", "urgent": False,
    })

    await set_tenant(db_session, practice.id)
    rows = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    )).scalars().all()
    assert len(rows) == 2
