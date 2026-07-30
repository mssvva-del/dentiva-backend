"""transfer_to_human escalates as an urgent callback — it never bridges a call.

Retell only connects the parties for its NATIVE transfer_call tool. Ours is
`type: custom`, so the old "transfer_initiated" + number answer made the agent
promise a connection that could not happen. These tests pin the honest contract.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import set_tenant
from app.models.callback_request import CallbackRequest
from app.models.practice import Practice
from tests.conftest import seed_practice


async def _transfer(client):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-call-1", "agent_id": "agent_tr"},
            "name": "transfer_to_human",
            "args": {"reason": "caller asked for a person"},
        },
    )


async def test_escalation_queues_urgent_callback_not_a_bridge(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Transfer Dental", clerk_org_id="org_tr1", clerk_user_id="user_tr1"
    )
    practice.transfer_phone_number = "+15551239999"
    practice_id = practice.id  # grab before commit expires the instance
    await db_session.commit()

    resp = await _transfer(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "callback_logged"
    # A number in the answer is what made the agent say "connecting you".
    assert "transfer_number" not in body

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    cb = (await db_session.execute(select(CallbackRequest))).scalars().all()
    assert len(cb) == 1 and cb[0].urgent is True


async def test_practice_me_exposes_transfer_number(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Transfer Dental 3", clerk_org_id="org_tr3", clerk_user_id="user_tr3"
    )
    resp = await client.patch(
        "/api/practice/me",
        json={"transfer_phone_number": "+15550001234"},
        headers={"X-Dev-Clerk-User-Id": "user_tr3", "X-Dev-Clerk-Org-Id": "org_tr3"},
    )
    assert resp.status_code == 200
    assert resp.json()["transfer_phone_number"] == "+15550001234"

    db_practice = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    assert db_practice.transfer_phone_number == "+15550001234"
