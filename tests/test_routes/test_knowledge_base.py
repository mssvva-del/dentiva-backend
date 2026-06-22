"""Tests for GET/PUT /api/knowledge-base.

Coverage:
  - GET returns null/empty for a fresh practice.
  - PUT then GET round-trip (providers + appointment_types preserved).
  - PUT requires MANAGE_SETTINGS (viewer → 403, no auth → 401/403).
  - Partial payload doesn't blow up (all-optional schema).

Uses the same helpers as test_onboarding.py:
  _fresh_onboarding_practice / _auth / seed_practice.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.practice import Practice
from app.models.user import User
from tests.conftest import seed_practice


def _auth(org: str, user: str) -> dict[str, str]:
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _fresh_onboarding_practice(db_session, *, slug: str):
    """Seed a practice in 'onboarding' status (mirrors test_onboarding.py helper)."""
    practice, owner = await seed_practice(
        db_session,
        name="Test Practice",
        clerk_org_id=f"org_{slug}",
        clerk_user_id=f"user_{slug}",
    )
    p = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    p.status = "onboarding"
    p.onboarding_step = 1
    await db_session.commit()
    return practice, owner


# ---------------------------------------------------------------------------
# GET — empty knowledge base
# ---------------------------------------------------------------------------

async def test_get_empty_knowledge_base(client, db_session):
    """A fresh practice returns knowledge_base: null (nothing saved yet)."""
    await _fresh_onboarding_practice(db_session, slug="kb1")
    h = _auth("org_kb1", "user_kb1")

    r = await client.get("/api/knowledge-base", headers=h)
    assert r.status_code == 200
    body = r.json()
    # Either null or an empty object — endpoint returns null for unset KB.
    assert "knowledge_base" in body
    assert body["knowledge_base"] is None


# ---------------------------------------------------------------------------
# PUT → GET round-trip
# ---------------------------------------------------------------------------

async def test_put_and_get_knowledge_base(client, db_session):
    """PUT a full-ish knowledge base, then GET it back and verify the values."""
    await _fresh_onboarding_practice(db_session, slug="kb2")
    h = _auth("org_kb2", "user_kb2")

    payload = {
        "providers": [
            {
                "name": "Dr. Sarah Smith",
                "type": "general",
                "days": ["mon", "wed", "fri"],
                "accepts_new": True,
            }
        ],
        "appointment_types": [
            {
                "name": "Cleaning",
                "minutes": 60,
                "provider_type": "hygienist",
                "new_patient": False,
            },
            {
                "name": "Emergency",
                "minutes": 30,
                "provider_type": "general",
                "new_patient": True,
            },
        ],
        "insurances": ["Delta Dental", "Cigna", "Aetna"],
        "self_pay": True,
        "policies": {
            "cancellation": "24 hours notice or $50 fee",
            "late": "15+ min late may be rescheduled",
            "new_patient": "Arrive 15 min early with ID and insurance card",
            "parking": "Free lot behind the building",
        },
        "emergency": {
            "triggers": ["bleeding", "trauma", "severe pain", "swelling"],
            "action": "transfer",
            "on_call_number": "+13105550000",
        },
    }

    put_r = await client.put("/api/knowledge-base", headers=h, json=payload)
    assert put_r.status_code == 200, put_r.text

    body = put_r.json()
    kb = body["knowledge_base"]
    assert kb is not None
    assert len(kb["providers"]) == 1
    assert kb["providers"][0]["name"] == "Dr. Sarah Smith"
    assert len(kb["appointment_types"]) == 2
    assert kb["appointment_types"][0]["name"] == "Cleaning"
    assert kb["insurances"] == ["Delta Dental", "Cigna", "Aetna"]
    assert kb["self_pay"] is True
    assert kb["policies"]["cancellation"] == "24 hours notice or $50 fee"
    assert kb["emergency"]["action"] == "transfer"
    assert kb["emergency"]["on_call_number"] == "+13105550000"

    # GET must return the same data.
    get_r = await client.get("/api/knowledge-base", headers=h)
    assert get_r.status_code == 200
    assert get_r.json()["knowledge_base"]["providers"][0]["name"] == "Dr. Sarah Smith"


async def test_put_writes_audit_log(client, db_session):
    """PUT /api/knowledge-base records an audit row with section counts."""
    practice, _owner = await _fresh_onboarding_practice(db_session, slug="kbaudit")
    h = _auth("org_kbaudit", "user_kbaudit")

    payload = {
        "providers": [{"name": "Dr. A"}, {"name": "Dr. B"}],
        "appointment_types": [{"name": "Cleaning"}],
        "insurances": ["Delta Dental"],
    }
    put_r = await client.put("/api/knowledge-base", headers=h, json=payload)
    assert put_r.status_code == 200, put_r.text

    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.practice_id == practice.id,
                AuditLog.action == "knowledge_base_updated",
            )
        )
    ).scalar_one()
    assert audit.resource_type == "practice"
    assert audit.audit_metadata["providers_count"] == 2
    assert audit.audit_metadata["appointment_types_count"] == 1
    assert audit.audit_metadata["insurances_count"] == 1


async def test_put_partial_knowledge_base(client, db_session):
    """Partial payload (only providers) saves without error; missing keys absent."""
    await _fresh_onboarding_practice(db_session, slug="kb3")
    h = _auth("org_kb3", "user_kb3")

    r = await client.put("/api/knowledge-base", headers=h, json={
        "providers": [{"name": "Dr. Jones", "type": "general"}],
    })
    assert r.status_code == 200
    kb = r.json()["knowledge_base"]
    assert kb["providers"][0]["name"] == "Dr. Jones"
    # Fields not sent should not appear (excluded_none serialisation).
    assert "insurances" not in kb or kb.get("insurances") is None


async def test_put_empty_payload_clears_knowledge_base(client, db_session):
    """PUT with an empty body sets knowledge_base back to null."""
    await _fresh_onboarding_practice(db_session, slug="kb4")
    h = _auth("org_kb4", "user_kb4")

    # First, set something.
    await client.put("/api/knowledge-base", headers=h, json={
        "insurances": ["Delta Dental"],
    })

    # Then clear it.
    r = await client.put("/api/knowledge-base", headers=h, json={})
    assert r.status_code == 200
    assert r.json()["knowledge_base"] is None


# ---------------------------------------------------------------------------
# Auth / permission enforcement
# ---------------------------------------------------------------------------

async def test_knowledge_base_requires_auth(client, db_session):
    """Requests with no auth headers must be rejected (401 or 403)."""
    # GET without credentials
    r = await client.get("/api/knowledge-base")
    assert r.status_code in (401, 403)

    # PUT without credentials
    r = await client.put("/api/knowledge-base", json={"insurances": ["Delta Dental"]})
    assert r.status_code in (401, 403)


async def test_put_knowledge_base_requires_manage_settings(client, db_session):
    """A viewer must be blocked from mutating the knowledge base (403)."""
    practice, _ = await _fresh_onboarding_practice(db_session, slug="kb5")

    # Add a viewer user to the same practice.
    viewer = User(
        id=uuid.uuid4(),
        clerk_user_id="user_kb5_viewer",
        practice_id=practice.id,
        email="viewer@kb5.com",
        role="viewer",
    )
    db_session.add(viewer)
    await db_session.commit()

    h = _auth("org_kb5", "user_kb5_viewer")
    r = await client.put("/api/knowledge-base", headers=h, json={
        "insurances": ["Delta Dental"],
    })
    assert r.status_code == 403


async def test_get_knowledge_base_viewer_can_read(client, db_session):
    """A viewer (read-only role) should be able to GET the knowledge base."""
    practice, _ = await _fresh_onboarding_practice(db_session, slug="kb6")

    # Seed KB as the owner first.
    owner_h = _auth("org_kb6", "user_kb6")
    await client.put("/api/knowledge-base", headers=owner_h, json={
        "self_pay": True,
    })

    # Add a viewer.
    viewer = User(
        id=uuid.uuid4(),
        clerk_user_id="user_kb6_viewer",
        practice_id=practice.id,
        email="viewer@kb6.com",
        role="viewer",
    )
    db_session.add(viewer)
    await db_session.commit()

    viewer_h = _auth("org_kb6", "user_kb6_viewer")
    r = await client.get("/api/knowledge-base", headers=viewer_h)
    assert r.status_code == 200
    assert r.json()["knowledge_base"]["self_pay"] is True
