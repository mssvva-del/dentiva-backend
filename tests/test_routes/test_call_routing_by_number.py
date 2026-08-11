"""Which clinic a phone call belongs to.

Routing matched practices.retell_agent_id. Nothing in the codebase ever wrote
that column — it appeared only in reads — so for a real phone call the match
never succeeded and everything rested on the fallback: "there is exactly one
practice in the database."

That fallback is true today and false the morning a second clinic onboards. From
that morning: check_availability answers with an empty list, which the agent
reads out as "we have nothing available"; book_appointment returns a shape with
no `booked` key at all; a callback is never written. No error, no alert. The
clinic hears from patients that the phone line is broken.

The key that IS populated was already there. Provisioning buys a Dentovox number
per clinic and writes it to practices.ai_phone_number, and the inbound webhook
routed on it correctly — while the event and tool webhook, two hundred lines
away, did not. Two implementations of the same question, and the correct one was
the one not being used for the calls that matter.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.call import Call
from tests.conftest import seed_practice

_DIALLED = "+16205550101"


async def _two_clinics(db_session):
    first, _ = await seed_practice(
        db_session, name="First Dental", clerk_org_id="org_r1", clerk_user_id="user_r1"
    )
    second, _ = await seed_practice(
        db_session, name="Second Dental", clerk_org_id="org_r2", clerk_user_id="user_r2"
    )
    # Only the second clinic answers on this number, as provisioning would set it.
    second.ai_phone_number = _DIALLED
    await db_session.commit()
    return first.id, second.id


async def test_the_number_that_was_dialled_decides_which_clinic(client, db_session):
    first_id, second_id = await _two_clinics(db_session)

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "route-1",
        "call": {"agent_id": "agent_shared", "from_number": "+15551112222",
                 "to_number": _DIALLED, "start_timestamp": 1748563200000},
    })

    await db_session.commit()
    db_session.expire_all()
    practice_id = (await db_session.execute(
        select(Call.practice_id).where(Call.retell_call_id == "route-1")
    )).scalar_one()
    assert practice_id == second_id
    assert practice_id != first_id, "the call landed on whichever clinic sorted first"


async def test_a_clinic_pointing_its_own_line_at_us_still_routes(client, db_session):
    """A practice can forward its published number to us rather than take a new
    one. Then the number dialled is theirs, not ours."""
    _, second_id = await _two_clinics(db_session)
    own_line = "+16205559999"
    from app.models.practice import Practice

    practice = (await db_session.execute(
        select(Practice).where(Practice.id == second_id)
    )).scalar_one()
    practice.ai_phone_number = None
    practice.phone_number = own_line
    await db_session.commit()

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "route-2",
        "call": {"agent_id": "agent_shared", "from_number": "+15551112222",
                 "to_number": own_line, "start_timestamp": 1748563200000},
    })

    await db_session.commit()
    db_session.expire_all()
    assert (await db_session.execute(
        select(Call.practice_id).where(Call.retell_call_id == "route-2")
    )).scalar_one() == second_id


async def test_an_unknown_number_with_two_clinics_refuses_rather_than_guesses(
    client, db_session
):
    """Guessing here is a cross-tenant leak: one clinic's patient recorded, and
    later answered, under another clinic's records. Refusing is a bad call; the
    alternative is a bad call plus a HIPAA incident."""
    await _two_clinics(db_session)

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "route-3",
        "call": {"agent_id": "agent_shared", "from_number": "+15551112222",
                 "to_number": "+16209998888", "start_timestamp": 1748563200000},
    })

    await db_session.commit()
    assert (await db_session.execute(
        select(Call).where(Call.retell_call_id == "route-3")
    )).scalars().first() is None


async def test_one_clinic_still_answers_a_number_nobody_configured(client, db_session):
    """The single-practice fallback stays: a pilot that has not finished
    provisioning must not be bricked by its own configuration gap."""
    practice, _ = await seed_practice(
        db_session, name="Only Dental", clerk_org_id="org_r9", clerk_user_id="user_r9"
    )
    practice_id = practice.id
    await db_session.commit()

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "route-4",
        "call": {"agent_id": "agent_shared", "from_number": "+15551112222",
                 "to_number": "+16207770000", "start_timestamp": 1748563200000},
    })

    await db_session.commit()
    db_session.expire_all()
    assert (await db_session.execute(
        select(Call.practice_id).where(Call.retell_call_id == "route-4")
    )).scalar_one() == practice_id
