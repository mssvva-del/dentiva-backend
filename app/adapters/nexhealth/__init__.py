"""NexHealth integration (Phase 1).

NexHealth is the aggregator over Dentrix/Eaglesoft/Open Dental/Denticon (+20),
so one adapter reaches every clinic Mike has. Phase 1 uses it for the
Reactivation Engine's data PULL (this package); the booking write-back side
(slots/create/reschedule/cancel) lands in block 8, gated on real keys + number.

The PULL is modeled as a ``ReactivationSource`` — a NEW capability on top of the
booking-oriented ``PMSAdapter``, because pulling a whole dormant-patient list is
a different concern from booking one appointment.

There was a ``get_reactivation_source()`` here that built a client straight from
the environment's keys. Nothing called it — but it was a trap: those keys name
ONE clinic's location, so the first person to wire the pull through it would have
had clinic B's reactivation campaign texting clinic A's dormant patients, with no
error anywhere. Every live PMS path goes through ``app.adapters.bridge`` instead,
which is where a practice is bound to its own credentials.
"""

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.adapters.nexhealth.source import ReactivationSource

__all__ = ["PMSReactivationRecord", "ReactivationSource"]
