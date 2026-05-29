"""Booking-related business logic backed by the PMS adapter."""

from __future__ import annotations

from app.adapters.open_dental import get_pms_adapter
from app.adapters.open_dental.models import AvailableSlot


async def find_available_slots(
    procedure: str,
    preferred_date: str,
    preferred_time_window: str | None,
) -> list[AvailableSlot]:
    """Return candidate appointment slots for a requested procedure/date.

    Iter 1: reads from the mock PMS adapter. The voice agent presents these to
    the caller; actual creation happens on a follow-up function_call (Phase 2).
    """
    adapter = get_pms_adapter()
    return await adapter.check_availability(
        date_from=preferred_date,
        date_to=preferred_date,
        preferred_window=preferred_time_window,
    )
