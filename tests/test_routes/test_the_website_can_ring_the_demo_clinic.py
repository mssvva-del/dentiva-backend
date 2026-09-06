"""The website's "talk to the receptionist" button.

Unauthenticated, and pinned to the seeded demo clinic — a visitor is nobody's
staff, so they may only reach the one practice that is nobody's either.
"""

from __future__ import annotations

from app.routes import voice
from tests.conftest import seed_practice


async def test_a_visitor_reaches_the_demo_clinic_and_nothing_else(client, db_session, monkeypatch):
    demo, _ = await seed_practice(
        db_session, name="Smile Dental (demo)", clerk_org_id=voice.DEMO_CLINIC_ORG,
        clerk_user_id="user_demo_site",
    )
    await seed_practice(
        db_session, name="A Real Clinic", clerk_org_id="org_real", clerk_user_id="user_real",
    )
    minted_for = []

    async def _fake_mint(practice):
        minted_for.append(practice.id)
        return {"access_token": "tok", "call_id": "web_1", "agent_id": "agent_x"}

    monkeypatch.setattr(voice, "_mint_web_call", _fake_mint)

    r = await client.post("/api/voice/public-web-call")
    assert r.status_code == 200, r.text
    assert r.json() == {"access_token": "tok", "call_id": "web_1", "agent_id": "agent_x"}
    assert minted_for == [demo.id]

