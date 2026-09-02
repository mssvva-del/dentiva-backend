"""Change an appointment in one place and have it change in both.

A booking lives in two calendars: ours and the practice's. Everything that edits
one has to edit the other, or the front desk is looking at an hour that is not
really free, or a patient at a time the clinic does not have.

Both callers — the clinic's own dashboard and our admin console — come through
here, so the propagation cannot be forgotten in one of them. It has been:
update_booking_status has set a booking to "cancelled" since the day it was
written and never once told the clinic's calendar, while cancel_in_pms sat
beside it, complete and uncalled.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking
from app.observability.alerts import record_alert
from app.services.reactivation.writeback import cancel_in_pms, move_in_pms

logger = logging.getLogger("dentiva.booking_edits")

# The PMS did what we asked, or had nothing to do. Anything else means our two
# calendars now disagree, and somebody has to be told.
_SETTLED = {"cancelled", "moved", "no_pms", "no_pms_record", "write_disabled"}


async def apply_cancellation(
    session: AsyncSession, practice_id: uuid.UUID, booking: Booking
) -> str:
    """Free the chair in the practice's calendar too.

    A cancellation that happens only here is the loss this product is sold to
    prevent, inverted: the patient is gone, the front desk still sees them
    booked, and the hour stays blocked against everyone else.
    """
    status = await cancel_in_pms(session, practice_id, booking)
    if status not in _SETTLED:
        record_alert("booking_cancel_not_synced", f"booking={booking.id} {status}")
        logger.warning(
            "cancellation not reflected in the PMS (%s) for booking %s",
            status, booking.id,
        )
    return status


async def apply_move(
    session: AsyncSession, practice_id: uuid.UUID, booking: Booking
) -> str:
    """Move it in the practice's calendar to where we just put it.

    Called AFTER the new time is committed here, so the two can only disagree in
    the direction where the clinic still holds the OLD slot — visible to the
    front desk — rather than a patient holding a time the clinic never had.
    """
    status = await move_in_pms(session, practice_id, booking)
    if status not in _SETTLED:
        record_alert("booking_move_not_synced", f"booking={booking.id} {status}")
        logger.warning(
            "move not reflected in the PMS (%s) for booking %s", status, booking.id
        )
    return status


async def slot_is_taken(
    session: AsyncSession,
    practice_id: uuid.UUID,
    *,
    when: datetime,
    duration_minutes: int,
    excluding: uuid.UUID,
) -> bool:
    """Is another confirmed appointment already sitting across this time?

    The database's unique constraint catches an exact collision. It cannot see an
    overlap — a 60-minute crown prep moved onto the half hour before a cleaning
    fits through it and puts two people in one chair. An editor that lets a human
    do deliberately what the agent is prevented from doing by accident is not a
    guard at all.
    """
    end = when + timedelta(minutes=duration_minutes or 60)
    rows = (await session.execute(
        select(Booking).where(
            Booking.practice_id == practice_id,
            Booking.status == "confirmed",
            Booking.id != excluding,
        )
    )).scalars().all()
    for other in rows:
        other_end = other.appointment_at + timedelta(
            minutes=other.duration_minutes or 60
        )
        if other.appointment_at < end and when < other_end:
            return True
    return False
