"""A clinic that exists only to be tested against, and must cost nothing.

Monitoring from outside can tell us the process is alive, the database answers,
isolation is on and the deploy landed. It cannot tell us the thing that matters —
whether a call still ends in a booking — without making one. And a synthetic
appointment in a live practice's calendar is worse than no monitoring, because a
receptionist has to explain a patient who does not exist.

So one practice is marked as existing for that purpose. The risk is not that the
canary breaks: it is that the canary breaks something else by being a perfectly
ordinary row in a table other code counts.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.call import Call
from app.models.practice import Practice
from tests.conftest import seed_practice

_REAL_NUMBER = "+16205551111"


async def _canary(db_session, *, number: str):
    practice, _ = await seed_practice(
        db_session, name="Dentovox Monitoring",
        clerk_org_id="org_canary", clerk_user_id="user_canary",
    )
    practice.is_canary = True
    practice.ai_phone_number = number
    practice.pms_system = "none"
    await db_session.commit()
    return practice.id


async def test_a_pilot_clinic_still_answers_after_a_canary_exists(client, db_session):
    """THE test. The canary is a real row with a real number, so a pilot with one
    customer suddenly looks like two practices — and the fallback that clinic's
    phone line depends on stops.

    A monitoring tenant taking the phones down for the customer it exists to
    protect would be a fine joke and a terrible morning.
    """
    real, _ = await seed_practice(
        db_session, name="Pilot Dental", clerk_org_id="org_pilot", clerk_user_id="user_pilot"
    )
    real_id = real.id
    await _canary(db_session, number="+16209990000")

    # A number nobody configured — exactly the pilot's situation mid-onboarding.
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "canary-1",
        "call": {"agent_id": "agent_shared", "from_number": "+15551112222",
                 "to_number": _REAL_NUMBER, "start_timestamp": 1748563200000},
    })

    await db_session.commit()
    db_session.expire_all()
    assert (await db_session.execute(
        select(Call.practice_id).where(Call.retell_call_id == "canary-1")
    )).scalar_one() == real_id


async def test_the_canary_answers_on_its_own_number(client, db_session):
    """It has to actually work, or the monitor is testing nothing."""
    await seed_practice(
        db_session, name="Pilot Dental 2", clerk_org_id="org_p2", clerk_user_id="user_p2"
    )
    canary_number = "+16209990000"
    canary_id = await _canary(db_session, number=canary_number)

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "canary-2",
        "call": {"agent_id": "agent_shared", "from_number": "+15551112222",
                 "to_number": canary_number, "start_timestamp": 1748563200000},
    })

    await db_session.commit()
    db_session.expire_all()
    assert (await db_session.execute(
        select(Call.practice_id).where(Call.retell_call_id == "canary-2")
    )).scalar_one() == canary_id


async def test_the_canary_is_not_counted_as_a_customer(db_session):
    """A monitoring tenant counted as a clinic is a number somebody eventually
    reports to someone else."""
    from sqlalchemy import func

    await seed_practice(
        db_session, name="Pilot Dental 3", clerk_org_id="org_p3", clerk_user_id="user_p3"
    )
    await _canary(db_session, number="+16209990000")

    customers = (await db_session.execute(
        select(func.count()).select_from(Practice).where(Practice.is_canary.is_(False))
    )).scalar_one()
    assert customers == 1


async def test_the_canary_has_no_pms_so_a_write_can_never_land_anywhere_real(
    db_session, monkeypatch
):
    """The load-bearing safety property. Every booking the monitor makes must
    stop at our database — a canary wired to a real PMS would put test
    appointments in somebody's calendar, which is the exact thing this design
    exists to prevent."""
    from app.adapters import bridge

    canary_id = await _canary(db_session, number="+16209990000")
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()

    settings = type("S", (), {
        "kolla_api_key": "k", "kolla_consumer_id": "consumers/1",
        "kolla_connector_id": "", "nexhealth_api_key": "k",
        "nexhealth_subdomain": "s", "nexhealth_location_id": "1",
    })()
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)

    # Every bridge fully configured, and still nothing to write to.
    assert bridge.bridge_name(practice) is None
    assert bridge.pms_client_for(practice) is None
