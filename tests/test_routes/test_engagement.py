"""Tests for the engagement analytics endpoint (waitlist + SMS funnel)."""

from __future__ import annotations

import uuid

from app.db import set_tenant
from app.models.audit_log import AuditLog
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _audit(db_session, practice_id, action, *, via=None):
    meta = {"via": via} if via else {}
    db_session.add(
        AuditLog(
            id=uuid.uuid4(),
            practice_id=practice_id,
            action=action,
            resource_type="test",
            resource_id=uuid.uuid4(),
            audit_metadata=meta,
        )
    )


async def test_engagement_counts_from_audit_logs(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Engage Dental", clerk_org_id="org_eng1", clerk_user_id="user_eng1"
    )
    await set_tenant(db_session, practice.id)
    # 3 joined, 2 notified, 1 sms-confirm, 1 sms-cancel, 1 recall, 1 opt-out.
    for _ in range(3):
        await _audit(db_session, practice.id, "waitlist_joined")
    for _ in range(2):
        await _audit(db_session, practice.id, "waitlist_notified")
    await _audit(db_session, practice.id, "booking_confirmed_sms")
    await _audit(db_session, practice.id, "booking_cancelled", via="sms")
    await _audit(db_session, practice.id, "booking_cancelled")  # non-SMS, excluded
    await _audit(db_session, practice.id, "recall_sms_sent")
    await _audit(db_session, practice.id, "sms_opt_out")
    await db_session.commit()

    resp = await client.get(
        "/api/dashboard/engagement", headers=_auth("org_eng1", "user_eng1")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["waitlist_joined"] == 3
    assert body["waitlist_notified"] == 2
    assert body["waitlist_conversion_rate"] == round(2 / 3, 3)
    assert body["sms_confirmed"] == 1
    assert body["sms_cancelled"] == 1  # only the via=sms one
    assert body["recall_sms_sent"] == 1
    assert body["sms_opt_outs"] == 1


async def test_engagement_empty_practice(client, db_session):
    await seed_practice(
        db_session, name="Engage Empty", clerk_org_id="org_eng2", clerk_user_id="user_eng2"
    )
    resp = await client.get(
        "/api/dashboard/engagement", headers=_auth("org_eng2", "user_eng2")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["waitlist_joined"] == 0
    assert body["waitlist_conversion_rate"] == 0.0


async def test_engagement_requires_auth(client, db_session):
    await seed_practice(
        db_session, name="Engage Auth", clerk_org_id="org_eng3", clerk_user_id="user_eng3"
    )
    resp = await client.get("/api/dashboard/engagement")
    assert resp.status_code == 401
