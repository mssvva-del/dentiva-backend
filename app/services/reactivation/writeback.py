"""PMS write-back via NexHealth (Phase 1, block 8).

The category-defining difference (spec 1.7): a reactivation booking is written
back into the REAL PMS calendar — not just flagged for the front desk. With:

  * **anti-double-book** — re-check the slot is still free in the PMS right before
    creating the appointment (it may have been taken since we offered it),
  * **graceful degradation** — if the PMS is down / its sync is stale, we DON'T
    crash or falsely confirm: leave the booking un-synced (pms_external_id NULL,
    a later retry writes it) and let the voice flow offer a callback instead.

Returns a status string so the caller (voice flow / worker) can react:
  'written' | 'conflict' | 'pms_unavailable' | 'pms_error'.

GATED on real NexHealth keys + the live booking flow (provider/operatory ids come
from the slot the agent offered). Built + unit-tested against a mocked NexHealth
API; wiring into the live book_appointment path is the 1-clinic live-loop.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.nexhealth.client import (
    NexHealthClient,
    NexHealthError,
    NexHealthUnavailable,
)
from app.db import set_tenant
from app.models.booking import Booking

logger = logging.getLogger("dentiva.reactivation.writeback")


def _slot_is_free(slots, start_time: str, provider_id: str) -> bool:
    """True if the PMS still shows our slot open (start_time + provider match)."""
    target = start_time[:16]  # minute precision; ignore seconds/zone formatting
    return any(
        s.provider_id == provider_id and (s.start_time or "")[:16] == target
        for s in slots
    )


async def write_back_booking(
    session: AsyncSession,
    practice_id: uuid.UUID,
    booking: Booking,
    *,
    patient_pms_id: str,
    provider_id: str,
    operatory_id: str | None = None,
    client: NexHealthClient | None = None,
) -> str:
    """Write a confirmed booking back to the PMS. See module docstring for status
    values. Sets ``booking.pms_external_id`` only on success."""
    client = client or NexHealthClient()
    await set_tenant(session, practice_id)
    start = booking.appointment_at.isoformat()
    appt_date = booking.appointment_at.date().isoformat()

    try:
        # 1. Anti-double-book: is the slot still open in the PMS right now?
        slots = await client.find_appointment_slots(
            start_date=appt_date, days=1, provider_ids=[provider_id]
        )
        if not _slot_is_free(slots, start, provider_id):
            logger.info("write-back conflict: slot taken in PMS for booking %s", booking.id)
            return "conflict"

        # 2. Create the appointment in the PMS and record its id on our booking.
        appt = await client.create_appointment(
            patient_pms_id=patient_pms_id,
            provider_id=provider_id,
            start_time=start,
            operatory_id=operatory_id,
            note="Booked via Dentovox reactivation",
        )
        booking.pms_external_id = appt.appointment_id
        await session.commit()
        return "written"

    except NexHealthUnavailable:
        # PMS down / stale sync — DO NOT crash or falsely confirm. Leave the
        # booking un-synced; a retry writes it later, and the voice flow offers a
        # callback rather than promising a calendar slot it couldn't secure.
        logger.warning("write-back deferred (PMS unavailable) for booking %s", booking.id)
        return "pms_unavailable"
    except NexHealthError:
        logger.warning("write-back failed (PMS 4xx) for booking %s", booking.id)
        return "pms_error"
