"""call_ended → billing metering must count each call's minutes ONCE.

Retell redelivers call_ended on timeout (documented in app/webhooks/retell.py),
and two app instances can receive the same redelivery close together. The
handler guards with ``usage_metered_at IS NULL`` under a ``SELECT ... FOR
UPDATE`` on the calls row specifically so a redelivery — sequential OR
concurrent — can't add the same call's minutes twice (silent over-metering is
a billing bug; under-metering the practice never notices, but double-metering
overcharges them). No prior test sent call_ended more than once.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.models.call import Call
from app.models.usage_record import UsageRecord
from tests.conftest import seed_practice

_CALL_ID = "retell-meter-001"


def _call_ended_payload(*, duration_s: int = 180):
    start = 1_748_563_200_000
    return {
        "event": "call_ended",
        "call_id": _CALL_ID,
        "call": {
            "start_timestamp": start,
            "end_timestamp": start + duration_s * 1000,
            "disconnection_reason": "user_hangup",
            "transcript": [{"role": "user", "content": "Thanks, bye."}],
        },
    }


async def test_call_ended_redelivered_sequentially_meters_once(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Meter Dental", clerk_org_id="org_meter1", clerk_user_id="user_meter1"
    )
    practice_id = practice.id  # capture before expire_all() below detaches the attribute
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": _CALL_ID,
        "call": {"from_number": "+15551230000", "to_number": "+15559876543",
                 "start_timestamp": 1_748_563_200_000},
    })

    # Retell redelivers the SAME call_ended twice (the real-world trigger: our
    # webhook handler was slow enough that Retell's delivery timed out and retried).
    r1 = await client.post("/webhooks/retell", json=_call_ended_payload())
    r2 = await client.post("/webhooks/retell", json=_call_ended_payload())
    assert r1.status_code == 200 and r2.status_code == 200

    await db_session.commit()
    db_session.expire_all()
    usage = (await db_session.execute(
        select(UsageRecord).where(UsageRecord.practice_id == practice_id)
    )).scalar_one()
    assert usage.calls_count == 1, "a redelivered call_ended must not count as a second call"
    assert usage.minutes_used == 3, "3 minutes must be metered exactly once, not twice"

    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == _CALL_ID)
    )).scalar_one()
    assert call.usage_metered_at is not None


async def test_call_ended_concurrent_redelivery_meters_once(client, db_session):
    """The harder case: both redeliveries land at (almost) the same instant, as
    they would from two app instances behind a load balancer. The FOR UPDATE
    lock on the calls row must serialize them so only the first to commit sees
    usage_metered_at IS NULL — the second must find it already stamped."""
    practice, _ = await seed_practice(
        db_session, name="Meter Race Dental", clerk_org_id="org_meter2",
        clerk_user_id="user_meter2",
    )
    practice_id = practice.id  # capture before expire_all() below detaches the attribute
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": _CALL_ID,
        "call": {"from_number": "+15551230001", "to_number": "+15559876543",
                 "start_timestamp": 1_748_563_200_000},
    })

    r1, r2 = await asyncio.gather(
        client.post("/webhooks/retell", json=_call_ended_payload()),
        client.post("/webhooks/retell", json=_call_ended_payload()),
    )
    assert r1.status_code == 200 and r2.status_code == 200

    await db_session.commit()
    db_session.expire_all()
    usage = (await db_session.execute(
        select(UsageRecord).where(UsageRecord.practice_id == practice_id)
    )).scalar_one()
    assert usage.calls_count == 1, (
        f"got calls_count={usage.calls_count} — the race let both redeliveries "
        "through and double-billed the practice for one call"
    )
    assert usage.minutes_used == 3
