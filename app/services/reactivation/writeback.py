"""PMS write-back — putting the appointment in the clinic's own calendar.

The category-defining difference (spec 1.7): a reactivation booking is written
back into the REAL PMS calendar — not just flagged for the front desk. With:

  * **anti-double-book** — re-check the slot is still free in the PMS right before
    creating the appointment (it may have been taken since we offered it),
  * **graceful degradation** — if the PMS is down / its sync is stale, we DON'T
    crash or falsely confirm: leave the booking un-synced (pms_external_id NULL,
    a later retry writes it) and let the voice flow offer a callback instead.

Returns a status string so the caller (voice flow / worker) can react:
  'written' | 'conflict' | 'pms_unavailable' | 'pms_error' | 'no_pms'.

Which PMS bridge answers is decided by app.adapters.bridge, not here. This file
used to construct a NexHealthClient unconditionally while the bridge preferred
Kolla, which meant that on a Kolla practice — our first customer's, on Eaglesoft
— every voice booking would have been created in our database, confirmed to the
patient by SMS, and never written to the calendar the front desk actually reads.
Silently: the failure surfaced as an alert in an in-process ring buffer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.kolla.client import KollaError, KollaUnavailable
from app.adapters.nexhealth.client import NexHealthError, NexHealthUnavailable
from app.db import set_tenant
from app.models.booking import Booking
from app.models.practice import Practice

# Every bridge reports the same two conditions, and the difference between them
# decides what the patient is told: unreachable means try again later, rejected
# means this booking is not going in without a human.
_PMS_UNAVAILABLE = (NexHealthUnavailable, KollaUnavailable)
_PMS_REJECTED = (NexHealthError, KollaError)

logger = logging.getLogger("dentiva.reactivation.writeback")


def _slot_is_free(slots, start_time: str, provider_id: str, operatory_id=None) -> bool:
    """Does the PMS still show this slot open?

    The time has to match. Beyond that we compare whichever identifier both sides
    actually carry, because the bridges disagree about what a slot belongs to:
    NexHealth schedules PROVIDERS and returns a provider id, Kolla schedules
    ROOMS and returns an operatory with the provider blank.

    Requiring a provider match, as this once did, made every Kolla slot look
    taken — so every booking on our first customer's PMS would have reported
    'conflict' and never been written, while the clinic's calendar sat empty and
    the patient held a confirmation.

    When neither side names anything, the time alone decides. That is weaker, and
    it is the right weakness: the cost of a false "free" is a double-booking the
    PMS itself will reject, while the cost of a false "taken" is an appointment
    that never reaches the clinic at all.
    """
    target = start_time[:16]  # minute precision; ignore seconds/zone formatting
    for slot in slots:
        if (slot.start_time or "")[:16] != target:
            continue
        if provider_id and slot.provider_id and slot.provider_id != provider_id:
            continue
        if operatory_id and slot.operatory_id and slot.operatory_id != operatory_id:
            continue
        return True
    return False


async def write_back_booking(
    session: AsyncSession,
    practice_id: uuid.UUID,
    booking: Booking,
    *,
    patient_pms_id: str,
    provider_id: str,
    operatory_id: str | None = None,
    client=None,
) -> str:
    """Write a confirmed booking back to the PMS. See module docstring for status
    values. Sets ``booking.pms_external_id`` only on success."""
    await set_tenant(session, practice_id)
    if client is None:
        from app.adapters.bridge import pms_client_for

        practice = (await session.execute(
            select(Practice).where(Practice.id == practice_id)
        )).scalar_one_or_none()
        client = pms_client_for(practice) if practice else None
        if client is None:
            # No bridge configured, or the practice has no PMS. The booking lives
            # in our book and that is the whole product for this clinic — saying
            # so is not the same as failing.
            return "no_pms"
    start = booking.appointment_at.isoformat()
    end = (
        booking.appointment_at + timedelta(minutes=booking.duration_minutes or 60)
    ).isoformat()
    appt_date = booking.appointment_at.date().isoformat()

    try:
        # 1. Anti-double-book: is the slot still open in the PMS right now?
        slots = await client.find_appointment_slots(
            start_date=appt_date, days=1, provider_ids=[provider_id]
        )
        if not _slot_is_free(slots, start, provider_id, operatory_id):
            logger.info("write-back conflict: slot taken in PMS for booking %s", booking.id)
            return "conflict"

        # 2. Create the appointment in the PMS and record its id on our booking.
        appt = await client.create_appointment(
            patient_pms_id=patient_pms_id,
            provider_id=provider_id,
            start_time=start,
            end_time=end,
            operatory_id=operatory_id,
            note="Booked via Dentovox",
        )
        booking.pms_external_id = appt.appointment_id
        await session.commit()
        return "written"

    except _PMS_UNAVAILABLE:
        # PMS down / stale sync — DO NOT crash or falsely confirm. Leave the
        # booking un-synced; a retry writes it later, and the voice flow offers a
        # callback rather than promising a calendar slot it couldn't secure.
        logger.warning("write-back deferred (PMS unavailable) for booking %s", booking.id)
        return "pms_unavailable"
    except _PMS_REJECTED:
        logger.warning("write-back failed (PMS 4xx) for booking %s", booking.id)
        return "pms_error"
