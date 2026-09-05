"""The agent said "you're all set" and never called book_appointment.

Live call: check_availability, the caller chose a time, the agent said "I'm
booking you for Thursday at nine — you're all set", the caller said thanks, the
call ended. No booking existed anywhere, and no screen could ever have shown
it, because there was nothing to show. The patient will arrive on Thursday.

Nothing can make a model call a tool it decided not to call. What can be done
deterministically is to notice, the moment the call ends, that a promise was
made with nothing behind it — and put a person on it.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.call import Call
from app.models.callback_request import CallbackRequest
from tests.conftest import seed_practice

PHONE = "+16175550190"


async def _clinic(db_session, tag):
    practice, _ = await seed_practice(
        db_session, name=f"Promise {tag}",
        clerk_org_id=f"org_pr{tag}", clerk_user_id=f"user_pr{tag}",
    )
    await db_session.commit()
    return practice


async def _call(client, practice, call_id, transcript):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": PHONE,
                 "to_number": practice.phone_number or "+16204559562",
                 "metadata": {"practice_id": str(practice.id)}},
    })
    return await client.post("/webhooks/retell", json={
        "event": "call_ended", "call_id": call_id,
        "call": {"call_id": call_id, "call_status": "ended",
                 "disconnection_reason": "user_hangup", "duration_ms": 95000,
                 "from_number": PHONE, "transcript_object": transcript,
                 "metadata": {"practice_id": str(practice.id)}},
    })


async def test_a_promise_with_nothing_behind_it_pages_the_clinic(client, db_session):
    practice = await _clinic(db_session, "a")
    r = await _call(client, practice, "prom-a", [
        {"role": "user", "content": "Thursday at nine works."},
        {"role": "agent", "content": "I'm booking you for Thursday at nine. You're all set!"},
        {"role": "user", "content": "Thanks, that's all."},
    ])
    assert r.status_code == 200, r.text

    from app.db import set_tenant
    await set_tenant(db_session, practice.id)
    cb = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    )).scalars().all()
    assert len(cb) == 1, "nobody was told to ring this person back"
    assert cb[0].urgent is True
    assert cb[0].phone == PHONE
    assert "no appointment was made" in cb[0].reason


async def test_in_spanish_too(client, db_session):
    practice = await _clinic(db_session, "b")
    await _call(client, practice, "prom-b", [
        {"role": "agent",
         "content": "Perfecto, tu cita está confirmada para el jueves a las nueve."},
        {"role": "user", "content": "Gracias."},
    ])
    from app.db import set_tenant
    await set_tenant(db_session, practice.id)
    cb = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    )).scalars().all()
    assert len(cb) == 1


async def test_a_call_that_made_no_promise_pages_nobody(client, db_session):
    """"Let me check what's open" is not a promise. Neither is the caller
    saying they are all set."""
    practice = await _clinic(db_session, "c")
    await _call(client, practice, "prom-c", [
        {"role": "agent", "content": "Let me check our morning openings for you."},
        {"role": "user", "content": "Actually I'm all set, I'll call back."},
        {"role": "agent", "content": "No problem — take care!"},
    ])
    from app.db import set_tenant
    await set_tenant(db_session, practice.id)
    cb = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    )).scalars().all()
    assert cb == []


async def test_a_promise_that_was_kept_pages_nobody(client, db_session):
    """A booking row for the call is the promise kept."""
    practice = await _clinic(db_session, "d")
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "prom-d",
        "call": {"call_id": "prom-d", "from_number": PHONE,
                 "to_number": practice.phone_number or "+16204559562",
                 "metadata": {"practice_id": str(practice.id)}},
    })
    b = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "prom-d",
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Nora", "patient_last_name": "Feld",
                 "patient_phone": PHONE, "procedure": "cleaning"},
    })
    assert b.json().get("booked") is True, b.json()
    await client.post("/webhooks/retell", json={
        "event": "call_ended", "call_id": "prom-d",
        "call": {"call_id": "prom-d", "call_status": "ended", "duration_ms": 120000,
                 "from_number": PHONE,
                 "transcript_object": [
                     {"role": "agent", "content": "You're all set for Thursday."}],
                 "metadata": {"practice_id": str(practice.id)}},
    })
    from app.db import set_tenant
    await set_tenant(db_session, practice.id)
    cb = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    )).scalars().all()
    assert cb == []
    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "prom-d")
    )).scalar_one()
    assert call.outcome == "booked"
