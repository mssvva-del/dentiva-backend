"""Tests for the callbacks API (list + status update).

Callbacks are created by the voice agent's create_callback_request tool and
must surface on the practice dashboard, urgent ones first.
"""

from __future__ import annotations

import uuid

from app.models.callback_request import CallbackRequest
from tests.conftest import seed_practice


async def _seed_callback(db_session, practice_id, **kw) -> CallbackRequest:
    cb = CallbackRequest(
        id=uuid.uuid4(),
        practice_id=practice_id,
        patient_first_name=kw.get("first_name", "Test"),
        phone=kw.get("phone", "+15551234567"),
        reason=kw.get("reason", "follow up"),
        urgent=kw.get("urgent", False),
        status=kw.get("status", "pending"),
    )
    db_session.add(cb)
    await db_session.commit()
    return cb


async def test_list_callbacks_urgent_first(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="CB List", clerk_org_id="org_cbl", clerk_user_id="user_cbl"
    )
    await _seed_callback(db_session, practice.id, reason="routine", urgent=False)
    await _seed_callback(
        db_session, practice.id, reason="bleeding", urgent=True, first_name="Ann",
        phone="+15559990351",
    )

    resp = await client.get(
        "/api/callbacks",
        headers={"X-Dev-Clerk-User-Id": "user_cbl", "X-Dev-Clerk-Org-Id": "org_cbl"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["pending_urgent"] == 1
    # Urgent callback bubbles to the top.
    assert body["callbacks"][0]["urgent"] is True
    assert body["callbacks"][0]["reason"] == "bleeding"
    # PII is redacted/masked for the frontend.
    assert body["callbacks"][0]["patient_name_redacted"] == "Ann"
    assert body["callbacks"][0]["phone_last4"] == "0351"


async def test_update_callback_status(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="CB Patch", clerk_org_id="org_cbp", clerk_user_id="user_cbp"
    )
    cb = await _seed_callback(db_session, practice.id, urgent=True)

    resp = await client.patch(
        f"/api/callbacks/{cb.id}",
        json={"status": "handled"},
        headers={"X-Dev-Clerk-User-Id": "user_cbp", "X-Dev-Clerk-Org-Id": "org_cbp"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "handled"

    # Now pending_urgent should drop to 0.
    listing = await client.get(
        "/api/callbacks",
        headers={"X-Dev-Clerk-User-Id": "user_cbp", "X-Dev-Clerk-Org-Id": "org_cbp"},
    )
    assert listing.json()["pending_urgent"] == 0


async def test_update_callback_invalid_status_422(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="CB Bad", clerk_org_id="org_cbb", clerk_user_id="user_cbb"
    )
    cb = await _seed_callback(db_session, practice.id)

    resp = await client.patch(
        f"/api/callbacks/{cb.id}",
        json={"status": "nonsense"},
        headers={"X-Dev-Clerk-User-Id": "user_cbb", "X-Dev-Clerk-Org-Id": "org_cbb"},
    )
    assert resp.status_code == 422


async def test_callbacks_tenant_isolation(client, db_session):
    """Practice B never sees Practice A's callbacks."""
    practice_a, _ = await seed_practice(
        db_session, name="CB A", clerk_org_id="org_cba", clerk_user_id="user_cba"
    )
    await seed_practice(
        db_session, name="CB B", clerk_org_id="org_cbb2", clerk_user_id="user_cbb2"
    )
    await _seed_callback(db_session, practice_a.id, urgent=True)

    resp = await client.get(
        "/api/callbacks",
        headers={"X-Dev-Clerk-User-Id": "user_cbb2", "X-Dev-Clerk-Org-Id": "org_cbb2"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
