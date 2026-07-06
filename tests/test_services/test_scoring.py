"""Reactivation value scoring (block 4) — score + prioritized queue."""

from __future__ import annotations

from datetime import date, timedelta

from app.adapters.nexhealth.mock import MockReactivationSource
from app.adapters.nexhealth.models import PMSReactivationRecord
from app.services.reactivation.scoring import (
    ScoringConfig,
    prioritize_for_segment,
    score,
)
from app.services.reactivation.segmentation import (
    DROPPED_TREATMENT,
    LAPSED,
    OVERDUE_RECALL,
)

_NOW = date(2026, 6, 24)


def _rec(**kw) -> PMSReactivationRecord:
    kw.setdefault("pms_external_id", "x")
    return PMSReactivationRecord(**kw)


def test_treatment_value_drives_score():
    r = _rec(treatment_plan_value_cents=150000)
    assert score(r, segments=[DROPPED_TREATMENT]) == 150000


def test_hygiene_ltv_added_for_recall_or_lapsed_only():
    r = _rec(treatment_plan_value_cents=0)
    # default hygiene LTV = 20000 cents
    assert score(r, segments=[OVERDUE_RECALL]) == 20000
    assert score(r, segments=[LAPSED]) == 20000
    # dropped-treatment-only patient gets no hygiene proxy
    assert score(r, segments=[DROPPED_TREATMENT]) == 0


def test_combined_treatment_plus_hygiene():
    r = _rec(treatment_plan_value_cents=120000)
    assert score(r, segments=[LAPSED, DROPPED_TREATMENT]) == 120000 + 20000


def test_weights_and_payer_multiplier_tunable():
    r = _rec(treatment_plan_value_cents=100000)
    cfg = ScoringConfig(treatment_weight=0.5, payer_multiplier=2.0, hygiene_weight=0)
    # (0.5 * 100000 + 0) * 2.0 = 100000
    assert score(r, segments=[LAPSED], config=cfg) == 100000


def test_prioritize_orders_highest_value_first():
    lapsed_dt = _NOW - timedelta(days=600)
    recs = [
        _rec(pms_external_id="low", last_visit_date=lapsed_dt),  # hygiene only
        _rec(pms_external_id="high", last_visit_date=lapsed_dt,
             treatment_plan_value_cents=300000),  # lapsed + big treatment
        _rec(pms_external_id="mid", last_visit_date=lapsed_dt,
             treatment_plan_value_cents=50000),
    ]
    ranked = prioritize_for_segment(recs, LAPSED, now=_NOW)
    assert [r.pms_external_id for _, r in ranked] == ["high", "mid", "low"]
    assert ranked[0][0] > ranked[1][0] > ranked[2][0]


async def test_prioritize_over_real_mock_source():
    recs = await MockReactivationSource().pull_reactivation_records()
    ranked = prioritize_for_segment(recs, LAPSED, now=_NOW)
    # nh-1001 (lapsed, $1800 treatment) must outrank a hygiene-only lapsed patient,
    # and the opted-out lapsed patient (nh-1004) is excluded entirely.
    assert ranked, "expected lapsed patients"
    assert ranked[0][1].pms_external_id == "nh-1001"
    assert all(r.contactable for _, r in ranked)
