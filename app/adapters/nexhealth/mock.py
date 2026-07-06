"""In-memory mock reactivation source.

Lets the Reactivation Engine (segmentation/scoring/scheduling) be built and
tested with NO real NexHealth access. Returns a small synthetic base that covers
all three segments + both languages, so downstream blocks have realistic shapes
to work against. Selected automatically when NexHealth keys are absent.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.adapters.nexhealth.source import ReactivationSource

# Anchor relative to a fixed reference so the synthetic data is deterministic
# (no Date.now() drift in tests). Callers compare against their own "now".
_REF = date(2026, 6, 24)


def _sample() -> list[PMSReactivationRecord]:
    return [
        # Lapsed (>18 months, no recall set) — high-value dropped treatment.
        PMSReactivationRecord(
            pms_external_id="nh-1001", first_name="Maria", last_name="Garcia",
            phone="+15551110001", email="maria@example.com", preferred_language="es",
            last_visit_date=_REF - timedelta(days=560),
            treatment_plan_value_cents=180000, balance_cents=0, contactable=True,
        ),
        # Overdue recall (due date in the past), english.
        PMSReactivationRecord(
            pms_external_id="nh-1002", first_name="John", last_name="Miller",
            phone="+15551110002", email=None, preferred_language="en",
            last_visit_date=_REF - timedelta(days=210),
            recall_due_date=_REF - timedelta(days=30),
            treatment_plan_value_cents=0, balance_cents=4500, contactable=True,
        ),
        # Active-ish, recall in future — should NOT segment as lapsed/overdue.
        PMSReactivationRecord(
            pms_external_id="nh-1003", first_name="Emily", last_name="Chen",
            phone="+15551110003", preferred_language="en",
            last_visit_date=_REF - timedelta(days=40),
            recall_due_date=_REF + timedelta(days=160),
            contactable=True,
        ),
        # Opted-out at the PMS — must be excluded by the campaign layer.
        PMSReactivationRecord(
            pms_external_id="nh-1004", first_name="Robert", last_name="Diaz",
            phone="+15551110004", preferred_language="es",
            last_visit_date=_REF - timedelta(days=400), contactable=False,
        ),
        # Dirty/partial record (no name, no phone) — must survive, never crash.
        PMSReactivationRecord(pms_external_id="nh-1005"),
    ]


class MockReactivationSource(ReactivationSource):
    async def pull_reactivation_records(
        self, *, updated_since: date | None = None, limit: int = 1000
    ) -> list[PMSReactivationRecord]:
        records = _sample()
        if updated_since is not None:
            # Mimic incremental: only patients with a last_visit on/after the cutoff.
            records = [
                r for r in records
                if r.last_visit_date is not None and r.last_visit_date >= updated_since
            ]
        return records[:limit]
