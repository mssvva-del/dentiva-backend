"""ReactivationSource — the data-pull capability for the Reactivation Engine.

A NEW abstraction on top of the booking-oriented PMSAdapter: booking is "one
appointment now", reactivation is "give me the whole dormant-patient list to
work". Kept separate so adapters that can book but not bulk-pull (and vice
versa) don't have to fake methods they don't support.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.adapters.nexhealth.models import PMSReactivationRecord


class ReactivationSource(ABC):
    @abstractmethod
    async def pull_reactivation_records(
        self, *, updated_since: date | None = None, limit: int = 1000
    ) -> list[PMSReactivationRecord]:
        """Pull patient records for reactivation segmentation/scoring.

        ``updated_since`` enables INCREMENTAL pulls (only patients changed since
        the last sync) so we don't re-fetch the whole base every run; None = full
        pull. ``limit`` caps the batch. Implementations MUST tolerate dirty/partial
        PMS data (skip or default per-field, never raise on one bad record).
        """
        ...
