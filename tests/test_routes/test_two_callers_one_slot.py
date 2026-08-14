"""Two people ring at the same moment and want the same last opening.

The handler has an ``except IntegrityError`` branch that turns a real collision
into "that time was just taken — want one of these instead?" plus a fresh list.
It has never been executed by a test: grepping the suite for "just taken"
returns nothing.

The existing double-booking test issues the two bookings one after the other, so
the second simply sees the slot as gone while computing availability and never
reaches the collision at all. Only two genuinely simultaneous requests do.

That matters because a mistake INSIDE the branch — a wrong attribute, a typo —
would be a 500 on exactly the call where two patients grab the last opening. A
500 mid-call is dead air, and the model fills dead air by inventing something.
"""

from __future__ import annotations

import asyncio

from app.adapters.open_dental.models import AvailableSlot
from tests.conftest import seed_practice

_SLOT_DATE = "2099-11-10"
_SLOT_TIME = "10:00"


async def _start(client, call_id, from_number):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"from_number": from_number, "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })


async def _book(client, call_id, phone, last_name):
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": "book_appointment",
        "args": {
            "patient_first_name": "Pat", "patient_last_name": last_name,
            "patient_phone": phone, "procedure": "cleaning",
            "preferred_date": _SLOT_DATE, "preferred_time_window": "morning",
            "preferred_time": _SLOT_TIME,
        },
    })
    return r.json()


async def test_the_second_caller_is_offered_another_time_not_an_error(
    client, db_session, monkeypatch
):
    """THE test. Both requests are told the same single slot is free, so both try
    to take it — which is what happens when two people ring at once about the
    last Tuesday morning."""
    from app.webhooks import retell

    await seed_practice(
        db_session, name="Race Dental", clerk_org_id="org_rc1", clerk_user_id="u_rc1"
    )

    async def _always_one_free(session, practice, **kwargs):
        return [AvailableSlot(date=_SLOT_DATE, time=_SLOT_TIME, provider="Dr. Smith")]

    monkeypatch.setattr(retell, "_open_slots", _always_one_free)

    await _start(client, "race-a", "+16205550001")
    await _start(client, "race-b", "+16205550002")

    first, second = await asyncio.gather(
        _book(client, "race-a", "+16205550001", "Alpha"),
        _book(client, "race-b", "+16205550002", "Beta"),
    )

    booked = [r for r in (first, second) if r.get("booked")]
    refused = [r for r in (first, second) if not r.get("booked")]

    assert len(booked) == 1, f"both callers were told yes: {first} | {second}"
    assert len(refused) == 1

    # The refusal has to be a sentence the agent can say, not an error.
    loser = refused[0]
    assert "error" not in loser, f"the collision surfaced as a failure: {loser}"
    assert loser.get("message"), "the agent was handed nothing to say"
    assert "just taken" in loser["message"].lower()
