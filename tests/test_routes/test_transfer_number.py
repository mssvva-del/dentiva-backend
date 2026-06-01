"""transfer_to_human resolves the practice's transfer destination."""

from __future__ import annotations

from sqlalchemy import select

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


async def test_transfer_returns_configured_number(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Transfer Dental", clerk_org_id="org_tr1", clerk_user_id="user_tr1"
    )
    practice.transfer_phone_number = "+15551239999"
    await db_session.commit()

    resp = await _transfer(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "transfer_initiated"
    assert body["transfer_number"] == "+15551239999"


async def test_transfer_falls_back_to_main_phone(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Transfer Dental 2", clerk_org_id="org_tr2", clerk_user_id="user_tr2"
    )
    # No transfer number set; main phone should be used.
    practice.phone_number = "+15557770000"
    await db_session.commit()

    resp = await _transfer(client)
    assert resp.status_code == 200
    assert resp.json()["transfer_number"] == "+15557770000"


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
