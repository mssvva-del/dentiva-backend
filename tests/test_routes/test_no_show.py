"""Tests for no-show tracking.

Staff mark a booking as a no-show via PATCH /api/bookings/{id}/status. That
writes a booking_no_show audit log (so the analytics activity feed counts it)
and is reflected in the per-patient no_show_count on the patients roster.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.patient import Patient
from tests.conftest import seed_practice


def _AUTH(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _seed_booking(db_session, practice_id, *, status="confirmed", ext="ns-1"):
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
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(),
        practice_id=practice_id,
        patient_id=patient.id,
        appointment_at=datetime.now(tz=UTC) - timedelta(hours=2),
        procedure_type="cleaning",
        provider_name="Dr. Smith",
        status=status,
    )
    db_session.add(booking)
    await db_session.commit()
    return patient, booking


async def test_mark_no_show_updates_status_and_audits(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="NoShow Dental", clerk_org_id="org_ns1", clerk_user_id="user_ns1"
    )
    _, booking = await _seed_booking(db_session, practice.id)

    resp = await client.patch(
        f"/api/bookings/{booking.id}/status",
        json={"status": "no_show"},
        headers=_AUTH("org_ns1", "user_ns1"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_show"

    refreshed = (
        await db_session.execute(
            select(Booking)
            .where(Booking.id == booking.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "no_show"

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "booking_no_show")
        )
    ).scalars().all()
    assert len(audits) == 1


async def test_invalid_status_rejected(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="NoShow Dental 2", clerk_org_id="org_ns2", clerk_user_id="user_ns2"
    )
    _, booking = await _seed_booking(db_session, practice.id, ext="ns-2")

    resp = await client.patch(
        f"/api/bookings/{booking.id}/status",
        json={"status": "bogus"},
        headers=_AUTH("org_ns2", "user_ns2"),
    )
    assert resp.status_code == 422


async def test_update_status_requires_auth(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="NoShow Dental 3", clerk_org_id="org_ns3", clerk_user_id="user_ns3"
    )
    _, booking = await _seed_booking(db_session, practice.id, ext="ns-3")
    resp = await client.patch(
        f"/api/bookings/{booking.id}/status", json={"status": "no_show"}
    )
    assert resp.status_code == 401


async def test_patient_no_show_count_surfaces(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="NoShow Dental 4", clerk_org_id="org_ns4", clerk_user_id="user_ns4"
    )
    _, booking = await _seed_booking(db_session, practice.id, ext="ns-4")

    # Mark it a no-show, then read the patients roster.
    await client.patch(
        f"/api/bookings/{booking.id}/status",
        json={"status": "no_show"},
        headers=_AUTH("org_ns4", "user_ns4"),
    )

    resp = await client.get("/api/patients", headers=_AUTH("org_ns4", "user_ns4"))
    assert resp.status_code == 200
    patients = resp.json()["patients"]
    assert len(patients) == 1
    assert patients[0]["no_show_count"] == 1


async def test_activity_counts_no_shows(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="NoShow Dental 5", clerk_org_id="org_ns5", clerk_user_id="user_ns5"
    )
    _, booking = await _seed_booking(db_session, practice.id, ext="ns-5")

    await client.patch(
        f"/api/bookings/{booking.id}/status",
        json={"status": "no_show"},
        headers=_AUTH("org_ns5", "user_ns5"),
    )

    resp = await client.get(
        "/api/dashboard/activity", headers=_AUTH("org_ns5", "user_ns5")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["no_show"] == 1
    # Today's bucket should carry the no-show.
    assert body["days"][-1]["no_show"] == 1
