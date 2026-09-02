"""A callback you cannot call back is not a feature.

Every clinic-facing list masked the patient's phone to its last four digits and
the name to an initial. That masking was a habit carried from our cross-tenant
admin views, where it belongs. On the practice's own screen it made three things
useless at once: a callback request nobody can return, a waitlist nobody can
fill when a slot opens, and a booking nobody can ring about.

The practice is the covered entity for these people. Withholding their number
from their own front desk protects nobody and stops the work.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.booking import Booking
from app.models.callback_request import CallbackRequest
from app.models.patient import Patient
from app.models.waitlist_entry import WaitlistEntry
from tests.conftest import seed_practice

PHONE = "+16175550142"


def _as(tag: str) -> dict[str, str]:
    return {"X-Dev-Clerk-User-Id": f"u_reach_{tag}",
            "X-Dev-Clerk-Org-Id": f"org_reach_{tag}"}


async def _patient(db, practice_id) -> Patient:
    p = Patient(
        id=uuid.uuid4(), practice_id=practice_id,
        first_name="Miriam", last_name="Okonkwo", phone=PHONE,
        pms_external_id=f"TEST-{uuid.uuid4().hex[:8]}",
    )
    db.add(p)
    await db.commit()
    return p


async def test_a_callback_carries_the_number_to_call(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Reach A", clerk_org_id="org_reach_a",
        clerk_user_id="u_reach_a",
    )
    db_session.add(CallbackRequest(
        id=uuid.uuid4(), practice_id=practice.id,
        patient_first_name="James", phone=PHONE,
        reason="Needs emergency tooth extraction", urgent=True, status="pending",
    ))
    await db_session.commit()

    r = await client.get("/api/callbacks", headers=_as("a"))
    assert r.status_code == 200, r.text
    row = r.json()["callbacks"][0]
    assert row["patient_phone"] == PHONE, "the clinic cannot return this call"
    assert row["patient_name"] == "James"
    # The masked fields stay for now so a dashboard mid-deploy keeps rendering.
    assert row["phone_last4"] == "0142"


async def test_a_waitlist_entry_carries_the_number_to_call(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Reach B", clerk_org_id="org_reach_b",
        clerk_user_id="u_reach_b",
    )
    patient = await _patient(db_session, practice.id)
    db_session.add(WaitlistEntry(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        procedure_type="cleaning", status="waiting",
    ))
    await db_session.commit()

    r = await client.get("/api/waitlist", headers=_as("b"))
    assert r.status_code == 200, r.text
    row = r.json()["entries"][0]
    # A waitlist exists to be phoned the moment somebody cancels.
    assert row["patient_phone"] == PHONE
    assert row["patient_name"] == "Miriam Okonkwo"


async def test_a_booking_carries_the_patient_and_their_number(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Reach C", clerk_org_id="org_reach_c",
        clerk_user_id="u_reach_c",
    )
    patient = await _patient(db_session, practice.id)
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=2),
        duration_minutes=45, procedure_type="cleaning",
        status="confirmed", source="ai_call",
    ))
    await db_session.commit()

    r = await client.get("/api/bookings", headers=_as("c"))
    assert r.status_code == 200, r.text
    row = r.json()["bookings"][0]
    assert row["patient_phone"] == PHONE, "cannot ring this patient to confirm"
    assert row["patient_name"] == "Miriam Okonkwo"
    assert row["patient_name_redacted"] == "Miriam O."
