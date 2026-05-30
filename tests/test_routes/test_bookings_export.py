"""Tests for GET /api/bookings/export endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db import set_tenant
from tests.conftest import seed_practice


async def _seed_booking(db_session, practice_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a minimal Patient + Booking and return (patient_id, booking_id)."""
    from app.models.booking import Booking
    from app.models.patient import Patient

    await set_tenant(db_session, practice_id)

    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=f"pms_{uuid.uuid4().hex[:8]}",
        first_name="Jane",
        last_name="Smith",
    )
    db_session.add(patient)
    await db_session.flush()

    booking = Booking(
        id=uuid.uuid4(),
        practice_id=practice_id,
        patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=1),
        duration_minutes=45,
        procedure_type="Whitening",
        provider_name="Dr. Adams",
        status="confirmed",
        source="ai_call",
    )
    db_session.add(booking)
    await db_session.commit()

    return patient.id, booking.id


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_export_requires_auth(client):
    resp = await client.get("/api/bookings/export")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_export_returns_csv(client, db_session):
    org_id = f"org_{uuid.uuid4().hex[:8]}"
    user_id = f"user_{uuid.uuid4().hex[:8]}"
    practice, _ = await seed_practice(
        db_session,
        name="Export Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    _, booking_id = await _seed_booking(db_session, practice.id)

    resp = await client.get(
        "/api/bookings/export",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )

    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    disposition = resp.headers.get("content-disposition", "")
    assert "bookings_" in disposition

    # Verify CSV structure
    content = resp.text
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) >= 1  # at least header
    header = lines[0]
    assert "ID" in header
    assert "Patient" in header
    assert "Appointment Date" in header
    assert "Status" in header
