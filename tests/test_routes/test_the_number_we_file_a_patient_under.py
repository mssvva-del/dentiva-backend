"""The agent was never told the number the caller is ringing from.

It asks "is this the best number for you?", the caller says yes, and the agent
then has to put something in the phone field — a number nobody has said out
loud. It invents one that looks like a phone number: at the first live clinic,
three unrelated callers were all filed under the SAME fabricated number. Their
reminders would have gone to a stranger, and when one of them rang back to
cancel, nothing could be found under the number she was calling from.

Two halves to the fix. The agent is now given the caller's number, and caller ID
wins here regardless of what the agent typed — unless the caller genuinely
dictated a different number, which people do.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db import set_tenant
from app.models.patient import Patient
from app.services.llm.dynamic_vars import build_dynamic_variables
from tests.conftest import seed_practice

CALLER = "+16175550142"


async def _calling(client, practice, call_id: str, from_number: str = CALLER):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": from_number,
                 "to_number": practice.phone_number or "+16204559562",
                 "metadata": {"practice_id": str(practice.id)}},
    })


async def _book(client, call_id: str, phone: str, first_name: str = "Ruth"):
    return await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": "book_appointment",
        "args": {"patient_first_name": first_name, "patient_last_name": "Delaney",
                 "patient_phone": phone, "procedure": "cleaning"},
    })


async def _only_patient(db_session, practice_id) -> Patient:
    await set_tenant(db_session, practice_id)
    rows = (await db_session.execute(
        select(Patient).where(Patient.practice_id == practice_id)
    )).scalars().all()
    assert len(rows) == 1, [p.first_name for p in rows]
    return rows[0]


async def test_the_agent_is_told_which_number_the_call_is_from(db_session):
    practice, _ = await seed_practice(
        db_session, name="Var Dental", clerk_org_id="org_cn", clerk_user_id="user_cn"
    )
    assert build_dynamic_variables(practice, CALLER)["caller_number"] == CALLER
    # A web call has no number, and the key must still be there: Retell
    # substitutes only the keys we send, so a missing one is spoken aloud as a
    # literal "{{caller_number}}".
    assert build_dynamic_variables(practice)["caller_number"] == ""


async def test_a_number_the_agent_invented_loses_to_caller_id(client, db_session):
    """The example from the tool's own description, which is what it reached for."""
    practice, _ = await seed_practice(
        db_session, name="Invented Dental",
        clerk_org_id="org_inv", clerk_user_id="user_inv",
    )
    await _calling(client, practice, f"inv-{uuid.uuid4().hex[:6]}")
    call_id = f"inv2-{uuid.uuid4().hex[:6]}"
    await _calling(client, practice, call_id)
    r = await _book(client, call_id, "+15551234567")
    assert r.status_code == 200, r.text

    patient = await _only_patient(db_session, practice.id)
    assert patient.phone == CALLER, "the patient was filed under a made-up number"


async def test_no_number_at_all_still_reaches_the_caller(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Silent Dental",
        clerk_org_id="org_sil", clerk_user_id="user_sil",
    )
    call_id = f"sil-{uuid.uuid4().hex[:6]}"
    await _calling(client, practice, call_id)
    assert (await _book(client, call_id, "")).status_code == 200

    patient = await _only_patient(db_session, practice.id)
    assert patient.phone == CALLER


async def test_a_number_the_caller_actually_dictates_is_kept(client, db_session):
    """People do give a different number — the office should ring the one they
    asked for, not the handset they happened to call from."""
    practice, _ = await seed_practice(
        db_session, name="Dictated Dental",
        clerk_org_id="org_dic", clerk_user_id="user_dic",
    )
    call_id = f"dic-{uuid.uuid4().hex[:6]}"
    await _calling(client, practice, call_id)
    assert (await _book(client, call_id, "+19785551133")).status_code == 200

    patient = await _only_patient(db_session, practice.id)
    assert patient.phone == "+19785551133"


async def test_a_web_call_keeps_the_number_the_caller_typed(client, db_session):
    """No caller ID exists, so what the caller gave is all there is."""
    practice, _ = await seed_practice(
        db_session, name="Web Dental",
        clerk_org_id="org_web", clerk_user_id="user_web",
    )
    call_id = f"web-{uuid.uuid4().hex[:6]}"
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "to_number": practice.phone_number or "+16204559562",
                 "metadata": {"practice_id": str(practice.id)}},
    })
    assert (await _book(client, call_id, "+19785551144")).status_code == 200

    patient = await _only_patient(db_session, practice.id)
    assert patient.phone == "+19785551144"
