"""Phase B2 — onboarding wizard: step save/resume + go-live + RBAC.

Drives a fresh 'onboarding' practice through all steps to 'active', and checks
validation + permission boundaries on the way.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.practice import Practice
from app.models.user import User
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _fresh_onboarding_practice(db_session, *, slug: str):
    """Seed a practice and put it back into 'onboarding' at step 1."""
    practice, owner = await seed_practice(
        db_session, name="Unset", clerk_org_id=f"org_{slug}", clerk_user_id=f"user_{slug}"
    )
    p = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    p.status = "onboarding"
    p.onboarding_step = 1
    await db_session.commit()
    return practice, owner


_HOURS = {
    "mon": {"open": "09:00", "close": "17:00"},
    "tue": {"open": "09:00", "close": "17:00"},
    "wed": None, "thu": None, "fri": None, "sat": None, "sun": None,
}


async def test_full_onboarding_flow_to_live(client, db_session):
    await _fresh_onboarding_practice(db_session, slug="ob1")
    h = _auth("org_ob1", "user_ob1")

    # state — starts at step 1, onboarding
    r = await client.get("/api/onboarding/state", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "onboarding"
    assert r.json()["onboarding_step"] == 1

    # step 1 clinic
    r = await client.put("/api/onboarding/clinic", headers=h, json={
        "name": "Bright Smiles", "address": "1 Main St", "timezone": "America/New_York",
    })
    assert r.status_code == 200
    assert r.json()["name"] == "Bright Smiles"
    assert r.json()["onboarding_step"] == 2

    # step 2 hours
    r = await client.put("/api/onboarding/hours", headers=h, json={"business_hours": _HOURS})
    assert r.status_code == 200
    assert r.json()["onboarding_step"] == 3

    # step 3 phone — forward + emergency transfer destination
    r = await client.put("/api/onboarding/phone", headers=h, json={
        "mode": "forward", "forward_number": "+13105551234",
        "transfer_number": "+13105550000",
    })
    assert r.status_code == 200
    assert r.json()["phone_number"] == "+13105551234"
    assert r.json()["transfer_phone_number"] == "+13105550000"
    assert r.json()["onboarding_step"] == 4

    # step 4 pms — skip (mock)
    r = await client.put("/api/onboarding/pms", headers=h, json={"pms_system": "none"})
    assert r.status_code == 200
    assert r.json()["pms_system"] == "none"
    assert r.json()["onboarding_step"] == 5

    # step 5 agent — EN+ES
    r = await client.put("/api/onboarding/agent", headers=h, json={
        "agent_name": "Anna", "voice": "cartesia-Hailey",
        "greeting": "Thanks for calling Bright Smiles!", "languages": ["en", "es"],
    })
    assert r.status_code == 200
    assert set(r.json()["languages_enabled"]) == {"en", "es"}
    assert r.json()["agent_settings"]["agent_name"] == "Anna"
    assert r.json()["onboarding_step"] == 6

    # BAA gate: complete is blocked until Terms + BAA accepted (HIPAA)
    blocked = await client.post("/api/onboarding/complete", headers=h)
    assert blocked.status_code == 403
    acc = await client.post("/api/onboarding/baa/accept", headers=h, json={
        "signer_name": "Dr. Jane Roe", "signer_title": "Owner",
    })
    assert acc.status_code == 200

    # complete → live
    r = await client.post("/api/onboarding/complete", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["onboarding_step"] == 0
    assert body["complete"] is True


async def test_phone_forward_requires_number(client, db_session):
    await _fresh_onboarding_practice(db_session, slug="ob2")
    h = _auth("org_ob2", "user_ob2")
    r = await client.put("/api/onboarding/phone", headers=h, json={"mode": "forward"})
    assert r.status_code in (400, 422)


async def test_phone_skip_clears_number(client, db_session):
    await _fresh_onboarding_practice(db_session, slug="ob3")
    h = _auth("org_ob3", "user_ob3")
    r = await client.put("/api/onboarding/phone", headers=h, json={"mode": "skip"})
    assert r.status_code == 200
    assert r.json()["phone_number"] is None


async def test_phone_skip_still_sets_transfer_number(client, db_session):
    """A clinic can skip phone forwarding but still set an emergency transfer
    destination — transfer_to_human must have somewhere to route."""
    await _fresh_onboarding_practice(db_session, slug="ob3b")
    h = _auth("org_ob3b", "user_ob3b")
    r = await client.put("/api/onboarding/phone", headers=h, json={
        "mode": "skip", "transfer_number": "+13105550000",
    })
    assert r.status_code == 200
    assert r.json()["phone_number"] is None
    assert r.json()["transfer_phone_number"] == "+13105550000"


async def test_phone_invalid_transfer_number_rejected(client, db_session):
    await _fresh_onboarding_practice(db_session, slug="ob3c")
    h = _auth("org_ob3c", "user_ob3c")
    r = await client.put("/api/onboarding/phone", headers=h, json={
        "mode": "skip", "transfer_number": "555-not-e164",
    })
    assert r.status_code in (400, 422)


async def test_invalid_hours_rejected(client, db_session):
    await _fresh_onboarding_practice(db_session, slug="ob4")
    h = _auth("org_ob4", "user_ob4")
    bad = dict(_HOURS, mon={"open": "9am", "close": "17:00"})
    r = await client.put("/api/onboarding/hours", headers=h, json={"business_hours": bad})
    assert r.status_code == 400  # validation envelope maps 422→400 body? check status


async def test_unsupported_language_rejected(client, db_session):
    await _fresh_onboarding_practice(db_session, slug="ob5")
    h = _auth("org_ob5", "user_ob5")
    r = await client.put("/api/onboarding/agent", headers=h, json={
        "agent_name": "Anna", "languages": ["ru"],
    })
    assert r.status_code in (400, 422)


async def test_complete_blocked_when_incomplete(client, db_session):
    # Fresh practice with nothing filled (default business_hours all-None,
    # no agent_settings) must not be allowed to go live.
    await _fresh_onboarding_practice(db_session, slug="ob6")
    # Wipe business hours to the empty default to simulate a not-yet-filled practice.
    p = (
        await db_session.execute(
            select(Practice).where(Practice.clerk_org_id == "org_ob6")
        )
    ).scalar_one()
    p.business_hours = {d: None for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    p.agent_settings = None
    await db_session.commit()

    h = _auth("org_ob6", "user_ob6")
    # Accept BAA first so we isolate the required-FIELD gate (not the BAA gate).
    await client.post("/api/onboarding/baa/accept", headers=h,
                      json={"signer_name": "X", "signer_title": "Owner"})
    r = await client.post("/api/onboarding/complete", headers=h)
    assert r.status_code in (400, 422)


async def test_onboarding_requires_manage_settings(client, db_session):
    # A viewer in the practice must be blocked from mutating onboarding.
    practice, _ = await _fresh_onboarding_practice(db_session, slug="ob7")
    db_session.add(User(
        id=uuid.uuid4(), clerk_user_id="user_ob7_viewer", practice_id=practice.id,
        email="v@ob7.com", role="viewer",
    ))
    await db_session.commit()

    h = _auth("org_ob7", "user_ob7_viewer")
    r = await client.put("/api/onboarding/clinic", headers=h, json={
        "name": "X", "timezone": "America/New_York",
    })
    assert r.status_code == 403


async def test_pms_choice_pages_us_so_connecting_is_true(client, db_session):
    """The UI tells the clinic their PMS connection is being set up. That only
    stays true if a human actually finds out — choosing a real PMS must alert."""
    from app.observability import alerts
    from tests.conftest import seed_practice

    await seed_practice(db_session, name="PmsAlert", clerk_org_id="org_pa", clerk_user_id="u_pa")
    await db_session.commit()
    h = {"X-Dev-Clerk-User-Id": "u_pa", "X-Dev-Clerk-Org-Id": "org_pa"}

    before = alerts.recent_alerts()["by_kind"].get("pms_connection_requested", 0)
    r = await client.put("/api/onboarding/pms", headers=h, json={"pms_system": "open_dental"})
    assert r.status_code == 200
    after = alerts.recent_alerts()["by_kind"].get("pms_connection_requested", 0)
    assert after == before + 1, "picking a PMS must page us — otherwise nothing connects"

    # "Skip for now" promises nothing, so it must NOT page us.
    r2 = await client.put("/api/onboarding/pms", headers=h, json={"pms_system": "none"})
    assert r2.status_code == 200
    assert alerts.recent_alerts()["by_kind"].get("pms_connection_requested", 0) == after
