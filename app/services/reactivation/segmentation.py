"""Reactivation segmentation (Phase 1, block 3).

Classifies pulled ``PMSReactivationRecord``s into the three dormant cohorts the
engine campaigns against:

  * LAPSED            — no visit for a long time (default ~18 months).
  * OVERDUE_RECALL    — past their recall/hygiene due date.
  * DROPPED_TREATMENT — has un-done/unscheduled treatment value.

Pure functions over the DTO — no DB, no PMS calls — so it's trivially testable
and reused by the campaign builder (block 5). Thresholds live in
``SegmentationConfig`` so a clinic (or we) can tune them without code changes.

WHY "unknown → not in segment": if a value is missing (e.g. no last_visit_date),
we do NOT assume the worst and contact the patient. A missing field means
"unknown", and we'd rather miss a borderline patient than cold-call someone we
can't justify reaching — important for TCPA/consent posture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.adapters.nexhealth.models import PMSReactivationRecord

LAPSED = "lapsed"
OVERDUE_RECALL = "overdue_recall"
DROPPED_TREATMENT = "dropped_treatment"
SEGMENTS = (LAPSED, OVERDUE_RECALL, DROPPED_TREATMENT)


@dataclass(frozen=True)
class SegmentationConfig:
    # A patient is "lapsed" after this many days without a visit. Default ~18
    # months (the spec's 12-18+ window); tunable per clinic.
    lapsed_after_days: int = 540
    # Minimum un-done treatment value (cents) to count as "dropped treatment".
    # >0 by default; a clinic can raise it to ignore trivial amounts.
    dropped_treatment_min_cents: int = 1


_DEFAULT = SegmentationConfig()


def classify(
    record: PMSReactivationRecord, *, now: date, config: SegmentationConfig = _DEFAULT
) -> list[str]:
    """Return every segment the record qualifies for (a patient can be in more
    than one — e.g. lapsed AND dropped-treatment)."""
    segments: list[str] = []
    lv = record.last_visit_date
    if lv is not None and (now - lv).days >= config.lapsed_after_days:
        segments.append(LAPSED)
    rd = record.recall_due_date
    if rd is not None and rd < now:
        segments.append(OVERDUE_RECALL)
    if record.treatment_plan_value_cents >= config.dropped_treatment_min_cents:
        segments.append(DROPPED_TREATMENT)
    return segments


def select_for_segment(
    records: list[PMSReactivationRecord],
    segment: str,
    *,
    now: date,
    config: SegmentationConfig = _DEFAULT,
    contactable_only: bool = True,
) -> list[PMSReactivationRecord]:
    """Filter pulled records down to one segment for a campaign.

    ``contactable_only`` drops patients the PMS marks un-contactable up front;
    our own sms_opt_out + TCPA quiet-hours are re-checked in the campaign layer
    (block 5) — this is a first, cheap exclusion, not the full consent gate.
    """
    if segment not in SEGMENTS:
        raise ValueError(f"unknown segment: {segment!r}")
    out: list[PMSReactivationRecord] = []
    for r in records:
        if contactable_only and not r.contactable:
            continue
        if segment in classify(r, now=now, config=config):
            out.append(r)
    return out
