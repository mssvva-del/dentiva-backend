"""transfer_to_human escalates as an urgent callback — it never bridges a call.

Retell only connects the parties for its NATIVE transfer_call tool. Ours is
`type: custom`, so the old "transfer_initiated" + number answer made the agent
promise a connection that could not happen. These tests pin the honest contract.
"""

from __future__ import annotations

import asyncio

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


async def test_urgent_callback_pages_the_clinic(client, db_session, monkeypatch):
    """A row in the dashboard is not a page — the team must be told."""
    from app.webhooks import retell as retell_mod

    paged: list[dict] = []

    async def _fake_page(**kwargs):
        paged.append(kwargs)
        return {"sent": True}

    monkeypatch.setattr(retell_mod, "page_clinic_urgent_callback", _fake_page)

    practice, _ = await seed_practice(
        db_session, name="Paged Dental", clerk_org_id="org_pg1", clerk_user_id="user_pg1"
    )
    practice.transfer_phone_number = "+15550009999"
    await db_session.commit()

    resp = await _transfer(client)
    assert resp.status_code == 200
    await asyncio.sleep(0)  # let the detached send task run
    assert len(paged) == 1
    assert paged[0]["to"] == "+15550009999"      # the clinic's own line, not the patient
    assert paged[0]["practice_name"] == "Paged Dental"


async def test_non_urgent_callback_does_not_page(client, db_session, monkeypatch):
    from app.webhooks import retell as retell_mod

    paged: list[dict] = []

    async def _fake_page(**kwargs):
        paged.append(kwargs)
        return {"sent": True}

    monkeypatch.setattr(retell_mod, "page_clinic_urgent_callback", _fake_page)
    await seed_practice(
        db_session, name="Quiet Dental", clerk_org_id="org_pg2", clerk_user_id="user_pg2"
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-call-2", "agent_id": "agent_tr2"},
            "name": "create_callback_request",
            "args": {"patient_first_name": "Ann", "patient_phone": "+15551110000",
                     "reason": "wants a price for whitening", "urgent": False},
        },
    )
    assert resp.status_code == 200
    await asyncio.sleep(0)
    assert paged == [], "routine callbacks must not text the clinic every time"
