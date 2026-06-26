"""Reactivation value scoring (Phase 1, block 4).

Orders the outreach queue so the most valuable dormant patients are contacted
first. The score combines (spec 1.4):

  * treatment_plan_value  — un-done treatment the patient already needs (biggest
    signal; it's real revenue sitting on the table).
  * hygiene LTV           — a recall/lapsed patient is due for a cleaning; we add
    a standard hygiene-visit value as proxy LTV.
  * payer mix             — better-paying plans rank higher.

All weights live in ``ScoringConfig`` so a clinic (or we) can tune what "valuable"
means without code changes. Pure function over the DTO — no DB, no PMS.

WHY payer is a placeholder today: payer/insurance is NOT in the current pull DTO
(needs a NexHealth enrichment endpoint — block 8). The weight exists in the config
so the formula is ready, but defaults to a neutral 1.0 until payer data is pulled.
Documented rather than faked.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.services.reactivation.segmentation import (
    LAPSED,
    OVERDUE_RECALL,
    SegmentationConfig,
    classify,
    select_for_segment,
)


@dataclass(frozen=True)
class ScoringConfig:
    # Weight on un-done treatment value (1.0 = count it dollar-for-dollar).
    treatment_weight: float = 1.0
    # Weight on the hygiene-LTV proxy added for recall-due/lapsed patients.
    hygiene_weight: float = 1.0
    # Standard hygiene-visit value (cents) used as the LTV proxy. ~$200 default.
    hygiene_visit_value_cents: int = 20_000
    # Payer-mix multiplier. PLACEHOLDER — neutral 1.0 until payer data is pulled
    # (block 8). Per-payer weights slot in here without touching the formula.
    payer_multiplier: float = 1.0


_DEFAULT = ScoringConfig()


def score(
    record: PMSReactivationRecord,
    *,
    segments: list[str],
    config: ScoringConfig = _DEFAULT,
) -> int:
    """Priority score for one record (points ≈ cents of recoverable value).
    Higher = contact first. ``segments`` is the record's classification (from
    segmentation.classify) so we know whether the hygiene LTV applies."""
    points = config.treatment_weight * record.treatment_plan_value_cents
    # A patient who's lapsed or overdue for recall is due for a cleaning — add the
    # hygiene LTV proxy. A dropped-treatment-only patient is valued by their plan.
    if LAPSED in segments or OVERDUE_RECALL in segments:
        points += config.hygiene_weight * config.hygiene_visit_value_cents
    points *= config.payer_multiplier
    return int(round(points))


def prioritize_for_segment(
    records: list[PMSReactivationRecord],
    segment: str,
    *,
    now,  # noqa: ANN001 — datetime.date
    seg_config: SegmentationConfig = SegmentationConfig(),
    score_config: ScoringConfig = _DEFAULT,
    contactable_only: bool = True,
) -> list[tuple[int, PMSReactivationRecord]]:
    """One call the campaign builder (block 5) uses: pull → this → enroll.

    Selects the segment's contactable records and returns them as
    ``(score, record)`` sorted highest-value first. Sort is stable, so equal
    scores keep pull order (deterministic queue).
    """
    selected = select_for_segment(
        records, segment, now=now, config=seg_config, contactable_only=contactable_only
    )
    scored = [
        (score(r, segments=classify(r, now=now, config=seg_config), config=score_config), r)
        for r in selected
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored
