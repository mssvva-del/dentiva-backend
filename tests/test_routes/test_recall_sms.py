"""Tests for the reactivation recall-SMS action.

Staff text a lapsed patient via POST /api/patients/{id}/recall-sms. We stub the
SMS sender so the test is deterministic regardless of the ambient Twilio config
— the endpoint still returns 200 with the outcome and writes a recall_sms_sent
audit log so the reactivation effort is auditable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.routes.patients as patients_routes
from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.patient import Patient
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


@pytest.fixture(autouse=True)
def _stub_recall_sms(monkeypatch):
    """Force a deterministic 'skipped' result (no network) for every test here."""

    async def _fake_send_recall_notice(**_kwargs):
        return {"skipped": "sms_disabled"}

    monkeypatch.setattr(
        patients_routes, "send_recall_notice", _fake_send_recall_notice
    )


async def _seed_patient(db_session, practice_id, *, ext="rc-1"):
    await set_tenant(db_session, practice_id)
    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=ext,
        first_name="Maria",
        last_name="Lopez",
        phone="+15557770000",
    )
    db_session.add(patient)
    await db_session.commit()
    return patient


async def test_recall_sms_skipped_when_disabled_but_audits(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Recall Dental", clerk_org_id="org_rc1", clerk_user_id="user_rc1"
    )
    patient = await _seed_patient(db_session, practice.id)

    resp = await client.post(
        f"/api/patients/{patient.id}/recall-sms",
        headers=_auth("org_rc1", "user_rc1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    # SMS disabled in tests → skipped, not sent, but the action is recorded.
    assert body["sent"] is False
    assert body["status"] == "skipped"

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "recall_sms_sent")
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].resource_id == patient.id


async def test_recall_sms_unknown_patient_404(client, db_session):
    await seed_practice(
        db_session, name="Recall Dental 2", clerk_org_id="org_rc2", clerk_user_id="user_rc2"
    )
    resp = await client.post(
        f"/api/patients/{uuid.uuid4()}/recall-sms",
        headers=_auth("org_rc2", "user_rc2"),
    )
    assert resp.status_code == 404


async def test_recall_sms_requires_auth(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Recall Dental 3", clerk_org_id="org_rc3", clerk_user_id="user_rc3"
    )
    patient = await _seed_patient(db_session, practice.id, ext="rc-3")
    resp = await client.post(f"/api/patients/{patient.id}/recall-sms")
    assert resp.status_code == 401
