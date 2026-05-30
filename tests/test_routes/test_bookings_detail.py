"""Tests for GET /api/bookings/:booking_id endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db import set_tenant
from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def _seed_booking(db_session, practice_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a minimal Patient + Booking and return (patient_id, booking_id).

    RLS on 'patients' requires the tenant setting to match the row's practice_id,
    so we call set_tenant before inserting.
    """
    from app.models.booking import Booking
    from app.models.patient import Patient

    await set_tenant(db_session, practice_id)

    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=f"pms_{uuid.uuid4().hex[:8]}",
        first_name="John",
        last_name="Doe",
    )
    db_session.add(patient)
    await db_session.flush()

    booking = Booking(
        id=uuid.uuid4(),
        practice_id=practice_id,
        patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=1),
        duration_minutes=60,
        procedure_type="Cleaning",
        provider_name="Dr. Smith",
        status="confirmed",
        source="ai_call",
    )
    db_session.add(booking)
    await db_session.commit()

    return patient.id, booking.id


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_get_booking_detail_requires_auth(client):
    resp = await client.get(f"/api/bookings/{uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_booking_detail_ok(client, db_session):
    org_id = _uid("org_bdet")
    user_id = _uid("user_bdet")
    practice, _ = await seed_practice(
        db_session,
        name="Detail Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    _, booking_id = await _seed_booking(db_session, practice.id)

    resp = await client.get(
        f"/api/bookings/{booking_id}",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(booking_id)
    assert data["procedure_type"] == "Cleaning"
    assert data["provider_name"] == "Dr. Smith"
    assert data["status"] == "confirmed"
    assert data["source"] == "ai_call"
    assert data["duration_minutes"] == 60
    assert "appointment_at" in data
    assert "created_at" in data
    # Patient name should be redacted (first initial + last name)
    assert data["patient_name_redacted"] is not None


@pytest.mark.asyncio
async def test_get_booking_detail_not_found(client, db_session):
    org_id = _uid("org_bnotfound")
    user_id = _uid("user_bnotfound")
    await seed_practice(
        db_session,
        name="NotFound Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        f"/api/bookings/{uuid.uuid4()}",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 404
    # Response is wrapped: {"error": {"code": ..., "message": ...}}
    body = resp.json()
    assert "error" in body
    assert "not found" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_get_booking_detail_wrong_tenant(client, db_session):
    """Booking from practice A must not be visible to practice B."""
    org_a = _uid("org_a")
    user_a = _uid("user_a")
    practice_a, _ = await seed_practice(
        db_session,
        name="Practice A",
        clerk_org_id=org_a,
        clerk_user_id=user_a,
    )

    org_b = _uid("org_b")
    user_b = _uid("user_b")
    await seed_practice(
        db_session,
        name="Practice B",
        clerk_org_id=org_b,
        clerk_user_id=user_b,
    )

    # Booking belongs to practice A
    _, booking_id = await _seed_booking(db_session, practice_a.id)

    # Queried by practice B → 404
    resp = await client.get(
        f"/api/bookings/{booking_id}",
        headers={"X-Dev-Clerk-User-Id": user_b, "X-Dev-Clerk-Org-Id": org_b},
    )
    assert resp.status_code == 404
