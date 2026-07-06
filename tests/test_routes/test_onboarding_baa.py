"""ONB-BAA — click-through Terms + BAA: fetch, accept (e-sign capture), go-live gate."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.baa_acceptance import BaaAcceptance
from app.models.practice import Practice
from app.models.user import User
from app.services.legal.baa import BAA_VERSION
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _fresh(db_session, *, slug: str):
    practice, owner = await seed_practice(
        db_session, name="BAA Co", clerk_org_id=f"org_{slug}", clerk_user_id=f"user_{slug}"
    )
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    p.status = "onboarding"
    p.onboarding_step = 6
    # Fill required fields so ONLY the BAA gate stands between us and go-live.
    p.business_hours = {"mon": {"open": "09:00", "close": "17:00"},
                        "tue": None, "wed": None, "thu": None, "fri": None,
                        "sat": None, "sun": None}
    p.languages_enabled = ["en"]
    p.agent_settings = {"agent_name": "Anna", "voice": "v", "greeting": "hi"}
    await db_session.commit()
    return practice


async def test_get_baa_shows_version_and_unaccepted(client, db_session):
    await _fresh(db_session, slug="baa1")
    h = _auth("org_baa1", "user_baa1")
    r = await client.get("/api/onboarding/baa", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == BAA_VERSION
    assert "BUSINESS ASSOCIATE" in body["text"].upper()
    assert body["accepted"] is False


async def test_accept_captures_signature_and_unblocks_golive(client, db_session):
    practice = await _fresh(db_session, slug="baa2")
    h = _auth("org_baa2", "user_baa2")

    # Blocked before acceptance.
    assert (await client.post("/api/onboarding/complete", headers=h)).status_code == 403

    acc = await client.post("/api/onboarding/baa/accept", headers=h, json={
        "signer_name": "  Dr. Jane Roe  ", "signer_title": "Practice Owner",
    })
    assert acc.status_code == 200

    # Signature row written with server-captured evidence.
    row = (await db_session.execute(
        select(BaaAcceptance).where(BaaAcceptance.practice_id == practice.id)
    )).scalar_one()
    assert row.document_version == BAA_VERSION
    assert row.signer_name == "Dr. Jane Roe"   # trimmed
    assert row.signer_title == "Practice Owner"
    assert row.accepted_by is not None
    # IP/user-agent captured server-side (ASGI transport → may be None/empty, but
    # the columns exist and are populated from the request, never the client body).

    # GET now reflects acceptance; complete succeeds.
    assert (await client.get("/api/onboarding/baa", headers=h)).json()["accepted"] is True
    done = await client.post("/api/onboarding/complete", headers=h)
    assert done.status_code == 200 and done.json()["status"] == "active"


async def test_accept_is_idempotent_per_version(client, db_session):
    practice = await _fresh(db_session, slug="baa3")
    h = _auth("org_baa3", "user_baa3")
    for _ in range(3):
        r = await client.post("/api/onboarding/baa/accept", headers=h, json={
            "signer_name": "Sam", "signer_title": "Manager",
        })
        assert r.status_code == 200
    rows = (await db_session.execute(
        select(BaaAcceptance).where(BaaAcceptance.practice_id == practice.id)
    )).scalars().all()
    assert len(rows) == 1  # no duplicate rows for the same version


async def test_accept_dedup_and_xff_ip(client, db_session):
    """Reviewer 🔴/🟡: UNIQUE (practice, version) keeps exactly one row even under
    repeated submits, and the signer IP comes from X-Forwarded-For (Railway proxy)."""
    practice = await _fresh(db_session, slug="baa7")
    h = _auth("org_baa7", "user_baa7")
    hdr = {**h, "X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    for _ in range(3):
        r = await client.post("/api/onboarding/baa/accept", headers=hdr,
                              json={"signer_name": "Sam", "signer_title": "Owner"})
        assert r.status_code == 200
    rows = (await db_session.execute(
        select(BaaAcceptance).where(BaaAcceptance.practice_id == practice.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].signer_ip == "203.0.113.7"  # first XFF hop, not the proxy


async def test_accept_validation(client, db_session):
    await _fresh(db_session, slug="baa4")
    h = _auth("org_baa4", "user_baa4")
    for bad in ({"signer_name": "", "signer_title": "Owner"},
                {"signer_name": "Sam", "signer_title": ""},
                {"signer_name": "Sam"}):
        r = await client.post("/api/onboarding/baa/accept", headers=h, json=bad)
        assert r.status_code in (400, 422)


async def test_baa_requires_manage_settings(client, db_session):
    practice = await _fresh(db_session, slug="baa5")
    db_session.add(User(
        id=uuid.uuid4(), clerk_user_id="user_baa5_viewer", practice_id=practice.id,
        email="v@baa5.com", role="viewer",
    ))
    await db_session.commit()
    h = _auth("org_baa5", "user_baa5_viewer")
    r = await client.post("/api/onboarding/baa/accept", headers=h, json={
        "signer_name": "V", "signer_title": "Viewer",
    })
    assert r.status_code == 403


async def test_baa_is_tenant_isolated(client, db_session):
    """A practice must not see another practice's BAA acceptance (RLS)."""
    p1 = await _fresh(db_session, slug="baa6a")
    await _fresh(db_session, slug="baa6b")
    await client.post("/api/onboarding/baa/accept", headers=_auth("org_baa6a", "user_baa6a"),
                      json={"signer_name": "A", "signer_title": "Owner"})
    # p2 has NOT accepted → its own view is unaccepted, and it can't see p1's row.
    r = await client.get("/api/onboarding/baa", headers=_auth("org_baa6b", "user_baa6b"))
    assert r.json()["accepted"] is False
    assert p1 is not None
