"""Reactivation segmentation (block 3) — classification + selection."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.adapters.nexhealth.mock import MockReactivationSource
from app.adapters.nexhealth.models import PMSReactivationRecord
from app.services.reactivation.segmentation import (
    DROPPED_TREATMENT,
    LAPSED,
    OVERDUE_RECALL,
    SegmentationConfig,
    classify,
    select_for_segment,
)

_NOW = date(2026, 6, 24)


def _rec(**kw) -> PMSReactivationRecord:
    return PMSReactivationRecord(pms_external_id="x", **kw)


def test_classify_lapsed():
    r = _rec(last_visit_date=_NOW - timedelta(days=600))
    assert classify(r, now=_NOW) == [LAPSED]


def test_classify_overdue_recall():
    r = _rec(last_visit_date=_NOW - timedelta(days=100), recall_due_date=_NOW - timedelta(days=5))
    assert classify(r, now=_NOW) == [OVERDUE_RECALL]


def test_classify_dropped_treatment():
    r = _rec(treatment_plan_value_cents=50000)
    assert classify(r, now=_NOW) == [DROPPED_TREATMENT]


def test_classify_multiple_segments():
    r = _rec(last_visit_date=_NOW - timedelta(days=600), treatment_plan_value_cents=120000)
    assert set(classify(r, now=_NOW)) == {LAPSED, DROPPED_TREATMENT}


def test_unknown_last_visit_is_not_lapsed():
    # Missing data must not be treated as "definitely lapsed".
    assert classify(_rec(last_visit_date=None), now=_NOW) == []


def test_future_recall_is_not_overdue():
    r = _rec(recall_due_date=_NOW + timedelta(days=30))
    assert OVERDUE_RECALL not in classify(r, now=_NOW)


def test_config_threshold_tunable():
    r = _rec(last_visit_date=_NOW - timedelta(days=400))
    assert classify(r, now=_NOW) == []  # default 540d → not lapsed at 400d
    loose = SegmentationConfig(lapsed_after_days=365)
    assert classify(r, now=_NOW, config=loose) == [LAPSED]  # 365d → lapsed


def test_select_for_segment_filters_contactable_and_segment():
    recs = [
        _rec(last_visit_date=_NOW - timedelta(days=600)),                     # lapsed, contactable
        _rec(last_visit_date=_NOW - timedelta(days=600), contactable=False),  # lapsed, opted out
        _rec(recall_due_date=_NOW - timedelta(days=10)),                      # overdue only
    ]
    picked = select_for_segment(recs, LAPSED, now=_NOW)
    assert len(picked) == 1  # opted-out + non-lapsed excluded


def test_select_unknown_segment_raises():
    with pytest.raises(ValueError, match="unknown segment"):
        select_for_segment([], "bogus", now=_NOW)


async def test_segments_over_real_mock_source():
    """End-to-end over the mock pull: the synthetic base yields each segment and
    excludes the opted-out patient from contactable selection."""
    recs = await MockReactivationSource().pull_reactivation_records()
    lapsed = select_for_segment(recs, LAPSED, now=_NOW)
    overdue = select_for_segment(recs, OVERDUE_RECALL, now=_NOW)
    dropped = select_for_segment(recs, DROPPED_TREATMENT, now=_NOW)
    assert lapsed and overdue and dropped  # all three populated
    # nh-1004 is lapsed but opted-out (contactable=False) → must be excluded.
    assert all(r.contactable for r in lapsed)
