"""Put a booking into the practice's calendar when it never got there.

An appointment can exist in our book and in no practice's software: the
write-back was refused, the patient could not be matched in their system, or the
slot carried no provider. Every one of those was an alert in our logs and a line
on the booking page reading "Not yet — this appointment is only in Dentovox",
with nothing anybody could press. A live clinic told us their calendar was
missing appointments before we noticed.

So the same push the booking flow performs, available again afterwards. It
re-reads the practice's own calendar for the day, finds the slot the appointment
sits in — which is where the provider and chair come from — and writes it. The
booking is untouched when it cannot: our copy is the one the patient is holding.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking
from app.models.practice import Practice
from app.services.availability import compute_pms_slots, slot_from_utc
from app.services.reactivation.writeback import write_back_booking

logger = logging.getLogger("dentiva.push_to_practice")

# What the front desk is told, in the words of the thing that has to happen next.
_NO_PATIENT = (
    "This patient isn't in your practice software yet, so there's nothing to "
    "attach the appointment to. Add them there, then try again."
)
_NO_SLOT = (
    "Your calendar doesn't show that time as open any more, so we can't put the "
    "appointment into it. Move it to a free time and it will go across."
)
_NO_PMS = "No practice software is connected to Dentovox."


async def push_booking(
    session: AsyncSession, practice: Practice, booking: Booking, patient_pms_id: str | None
) -> tuple[str, str | None]:
    """Try to create this booking in the practice's calendar.

    Returns ``(outcome, note)`` — the note is what the booking should carry, or
    None once the two calendars agree.
    """
    if not patient_pms_id or patient_pms_id.startswith("VOICE-"):
        # A stub id we invented for a caller their system has never seen.
        return "no_patient", _NO_PATIENT

    local_date, local_time = slot_from_utc(booking.appointment_at, practice.timezone)
    slots = await compute_pms_slots(
        practice, preferred_date=local_date, procedure=booking.procedure_type or "",
        days_ahead=1,
    )
    if slots is None:
        return "no_pms", _NO_PMS

    match = next(
        (s for s in slots if s.date == local_date and s.time == local_time), None
    )
    if match is None or not match.prov_num:
        return "no_slot", _NO_SLOT

    status = await write_back_booking(
        session, practice.id, booking,
        patient_pms_id=patient_pms_id,
        provider_id=match.prov_num,
        operatory_id=match.op_num,
    )
    logger.info("push to practice %s for booking %s", status, booking.id)
    if status == "written":
        return status, None
    if status == "conflict":
        return status, _NO_SLOT
    if status in ("no_pms", "write_disabled"):
        return status, _NO_PMS
    return status, "Your practice software refused it. Try again in a moment."
