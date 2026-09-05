"""Two questions to the same calendar, two answers.

Availability asked the practice for openings of the visit's length — a
45-minute cleaning — and offered 9:45, free until the 10:30 next door. The
write-back then asked the same calendar for 60-minute openings, found no 9:45
in that list, and refused the booking as a "conflict". The caller had already
been told they were booked; the appointment never reached the practice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.adapters.nexhealth.models import NexHealthAppointment, NexHealthSlot
from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.services.reactivation.writeback import write_back_booking
from tests.conftest import seed_practice


class _CalendarThatAnswersByLength:
    """9:45 fits 45 minutes and not 60 — the 10:30 next door is real."""

    def __init__(self):
        self.asked: list[int] = []

    async def find_appointment_slots(self, *, start_date, days=1, provider_ids=None,
                                     slot_length=60):
        self.asked.append(slot_length)
        if slot_length <= 45:
            return [NexHealthSlot(start_time="2099-09-08T13:45:00Z", end_time=None,
                                  provider_id="7", operatory_id="2")]
        return []

    async def create_appointment(self, **kw):
        return NexHealthAppointment(appointment_id="1680600001", start_time="2099-09-08T13:45:00Z")


async def test_a_45_minute_visit_is_checked_as_45_minutes(db_session):
    practice, _ = await seed_practice(
        db_session, name="Length Dental", clerk_org_id="org_len", clerk_user_id="user_len"
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(id=uuid.uuid4(), practice_id=practice.id, pms_external_id="88",
                      first_name="Ivy", last_name="Marsh", phone="+15551239100")
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
                      appointment_at=datetime(2099, 9, 8, 13, 45, tzinfo=UTC),
                      duration_minutes=45, procedure_type="cleaning", status="confirmed")
    db_session.add(booking)
    await db_session.commit()

    calendar = _CalendarThatAnswersByLength()
    status = await write_back_booking(
        db_session, practice.id, booking,
        patient_pms_id="88", provider_id="7", operatory_id="2", client=calendar,
    )

    assert calendar.asked == [45], "the write-back asked a different question than availability"
    assert status == "written", status
    assert booking.pms_external_id == "1680600001"
