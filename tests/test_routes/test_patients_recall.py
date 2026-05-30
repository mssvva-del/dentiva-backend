"""Tests for GET /api/patients/recall and bookings pagination (has_more)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db import set_tenant
from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def _seed_booking_and_patient(
    db_session,
    practice_id: uuid.UUID,
    *,
    appointment_at: datetime,
    status: str = "confirmed",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a Patient + Booking and return (patient_id, booking_id)."""
    from app.models.booking import Booking
    from app.models.patient import Patient

    await set_tenant(db_session, practice_id)

    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=f"pms_{uuid.uuid4().hex[:8]}",
        first_name="Jane",
        last_name="Recall",
    )
    db_session.add(patient)
    await db_session.flush()

    booking = Booking(
        id=uuid.uuid4(),
        practice_id=practice_id,
        patient_id=patient.id,
        appointment_at=appointment_at,
        duration_minutes=30,
        procedure_type="Checkup",
        provider_name="Dr. Recall",
        status=status,
        source="ai_call",
    )
    db_session.add(booking)
    await db_session.commit()

    return patient.id, booking.id


# ─────────────────────────────────────────────────────────────────────────────
# /api/patients/recall tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_recall_requires_auth(client):
    """Unauthenticated request must return 401."""
    resp = await client.get("/api/patients/recall")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_recall_returns_list(client, db_session):
    """Authenticated request returns a valid RecallResponse (list may be empty)."""
    org_id = _uid("org_recall")
    user_id = _uid("user_recall")
    await seed_practice(
        db_session,
        name="Recall Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/patients/recall",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Validate response structure
    assert "patients" in data
    assert "total" in data
    assert "recall_threshold_months" in data
    assert isinstance(data["patients"], list)
    assert isinstance(data["total"], int)
    assert data["recall_threshold_months"] == 6  # default

    # If any patients returned, validate their shape
    for p in data["patients"]:
        assert "patient_id" in p
        assert "last_visit_date" in p
        assert "months_since_visit" in p


@pytest.mark.asyncio
async def test_recall_custom_threshold(client, db_session):
    """threshold_months param is echoed back in response."""
    org_id = _uid("org_thresh")
    user_id = _uid("user_thresh")
    await seed_practice(
        db_session,
        name="Threshold Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/patients/recall?threshold_months=12",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recall_threshold_months"] == 12


# ─────────────────────────────────────────────────────────────────────────────
# /api/bookings pagination — has_more
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bookings_list_has_more_false(client, db_session):
    """GET /api/bookings returns has_more=false when result count < limit."""
    org_id = _uid("org_pgn")
    user_id = _uid("user_pgn")
    practice, _ = await seed_practice(
        db_session,
        name="Pagination Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    # Seed a single upcoming booking (within default 30-day window)
    await _seed_booking_and_patient(
        db_session,
        practice.id,
        appointment_at=datetime.now(UTC) + timedelta(days=3),
        status="confirmed",
    )

    resp = await client.get(
        "/api/bookings",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "has_more" in data
    # 1 booking with default limit=25 → has_more must be False
    assert data["has_more"] is False
    assert data["total"] >= 1
