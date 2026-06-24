"""NexHealth integration (Phase 1).

NexHealth is the aggregator over Dentrix/Eaglesoft/Open Dental/Denticon (+20),
so one adapter reaches every clinic Mike has. Phase 1 uses it for the
Reactivation Engine's data PULL (this package); the booking write-back side
(slots/create/reschedule/cancel) lands in block 8, gated on real keys + number.

The PULL is modeled as a ``ReactivationSource`` — a NEW capability on top of the
booking-oriented ``PMSAdapter``, because pulling a whole dormant-patient list is
a different concern from booking one appointment.
"""

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.adapters.nexhealth.source import ReactivationSource


def get_reactivation_source() -> ReactivationSource:
    """Return the reactivation data source.

    Real NexHealth client when keys are configured; otherwise the in-memory mock
    so the Reactivation Engine can be built and tested without live access.
    """
    from app.config import get_settings

    s = get_settings()
    if s.nexhealth_api_key and s.nexhealth_subdomain and s.nexhealth_location_id:
        from app.adapters.nexhealth.client import NexHealthClient

        return NexHealthClient()
    from app.adapters.nexhealth.mock import MockReactivationSource

    return MockReactivationSource()


__all__ = ["PMSReactivationRecord", "ReactivationSource", "get_reactivation_source"]
