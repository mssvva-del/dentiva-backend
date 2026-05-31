"""Tests for the patient detail endpoint (timeline)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.waitlist_entry import WaitlistEntry
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def test_patient_detail_returns_timeline(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Detail Dental", clerk_org_id="org_pd1", clerk_user_id="user_pd1"
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="pd-1",
        first_name="Maria", last_name="Lopez", phone="+15557770000",
    )
    db_session.add(patient)
    await db_session.flush()

    now = datetime.now(tz=UTC)
    db_session.add_all([
        Booking(
            id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
            appointment_at=now - timedelta(days=200), procedure_type="cleaning",
            provider_name="Dr. Smith", status="completed",
        ),
        Booking(
            id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
            appointment_at=now - timedelta(days=10), procedure_type="exam",
            provider_name="Dr. Smith", status="no_show",
        ),
        Booking(
            id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
            appointment_at=now + timedelta(days=5), procedure_type="filling",
            provider_name="Dr. Smith", status="confirmed",
        ),
    ])
    db_session.add(
        WaitlistEntry(
            id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
            procedure_type="cleaning", status="waiting",
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/patients/{patient.id}", headers=_auth("org_pd1", "user_pd1")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name_redacted"] == "Maria L."
    assert body["total_visits"] == 1
    assert body["no_show_count"] == 1
    assert body["status"] == "upcoming"
    assert len(body["bookings"]) == 3
    # Newest-first ordering.
    assert body["bookings"][0]["status"] == "confirmed"
    assert len(body["waitlist"]) == 1
    assert body["sms_opt_out"] is False


async def test_patient_detail_404_for_unknown(client, db_session):
    await seed_practice(
        db_session, name="Detail Dental 2", clerk_org_id="org_pd2", clerk_user_id="user_pd2"
    )
    resp = await client.get(
        f"/api/patients/{uuid.uuid4()}", headers=_auth("org_pd2", "user_pd2")
    )
    assert resp.status_code == 404


async def test_patient_detail_requires_auth(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Detail Dental 3", clerk_org_id="org_pd3", clerk_user_id="user_pd3"
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="pd-3",
        first_name="Ann", phone="+15551112222",
    )
    db_session.add(patient)
    await db_session.commit()
    resp = await client.get(f"/api/patients/{patient.id}")
    assert resp.status_code == 401


async def test_recall_route_not_shadowed_by_detail(client, db_session):
    # GET /api/patients/recall must still hit the recall endpoint, not be
    # captured by GET /api/patients/{patient_id}.
    await seed_practice(
        db_session, name="Detail Dental 4", clerk_org_id="org_pd4", clerk_user_id="user_pd4"
    )
    resp = await client.get(
        "/api/patients/recall", headers=_auth("org_pd4", "user_pd4")
    )
    assert resp.status_code == 200
    assert "recall_threshold_months" in resp.json()
