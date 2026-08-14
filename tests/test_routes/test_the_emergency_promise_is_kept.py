"""The emergency lock says the team is being notified. Somebody has to be.

When the deterministic keyword scan trips, the lock refuses to schedule and
answers the caller: "Our team is being notified right now and will call you
immediately." That sentence is the only thing a person with bleeding or swelling
is guaranteed to hear.

Behind it there was nothing. The block wrote no callback row, sent no page, and
raised no alert — whether anyone was actually told depended on the model going on
to call create_callback_request. The lock's whole premise is that the model
cannot be trusted in this moment, which is why it is a keyword scan and not an
instruction in a prompt.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.callback_request import CallbackRequest
from tests.conftest import seed_practice

_CALLER = "+16205551234"


async def _call_started(client, call_id):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"from_number": _CALLER, "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })


async def _tool(client, call_id, name, args):
    return await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": name, "args": args,
    })


async def test_the_lock_itself_raises_the_callback(client, db_session):
    """THE test. The caller mentions bleeding while asking for an appointment.
    Scheduling is refused, they are told the team knows — and the team is told."""
    practice, _ = await seed_practice(
        db_session, name="ER Dental", clerk_org_id="org_er1", clerk_user_id="u_er1"
    )
    practice_id = practice.id
    await _call_started(client, "er-1")

    body = (await _tool(client, "er-1", "book_appointment", {
        "patient_first_name": "Ann", "patient_last_name": "Lee",
        "patient_phone": _CALLER, "procedure": "cleaning",
        "preferred_date": "2099-11-10", "preferred_time_window": "morning",
        "reason": "my gum is bleeding and won't stop",
    })).json()
    assert body.get("blocked") is True, body

    db_session.expire_all()
    rows = (await db_session.execute(
        select(CallbackRequest).where(CallbackRequest.practice_id == practice_id)
    )).scalars().all()
    assert len(rows) == 1, "the caller was told the team knows, and nothing was written"
    assert rows[0].urgent is True
    assert rows[0].status == "pending"
    assert rows[0].phone == _CALLER


async def test_the_callback_tool_is_not_doubled_by_the_lock(client, db_session):
    """When the trigger IS create_callback_request, that handler writes its own
    row a moment later. Two rows means two pages for one bleeding patient, and a
    front desk that learns to skim them."""
    practice, _ = await seed_practice(
        db_session, name="ER Dental 2", clerk_org_id="org_er2", clerk_user_id="u_er2"
    )
    practice_id = practice.id
    await _call_started(client, "er-2")

    await _tool(client, "er-2", "create_callback_request", {
        "patient_name": "Ann Lee", "patient_phone": _CALLER,
        "reason": "swelling since last night", "urgent": True,
    })

    db_session.expire_all()
    count = (await db_session.execute(
        select(func.count()).select_from(CallbackRequest)
        .where(CallbackRequest.practice_id == practice_id)
    )).scalar_one()
    assert count == 1, "the lock and the tool each wrote a row for one emergency"


async def test_an_ordinary_call_raises_nothing(client, db_session):
    """A callback for every booking would bury the ones that matter."""
    practice, _ = await seed_practice(
        db_session, name="Calm Dental", clerk_org_id="org_er3", clerk_user_id="u_er3"
    )
    practice_id = practice.id
    await _call_started(client, "er-3")
    await _tool(client, "er-3", "check_availability", {
        "procedure": "cleaning", "preferred_date": "2099-11-10",
    })

    db_session.expire_all()
    count = (await db_session.execute(
        select(func.count()).select_from(CallbackRequest)
        .where(CallbackRequest.practice_id == practice_id)
    )).scalar_one()
    assert count == 0
