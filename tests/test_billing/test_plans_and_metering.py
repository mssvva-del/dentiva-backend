"""Phase D — plan catalog math + usage metering."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.billing.metering import _month_bounds, record_call_usage, record_sms_usage
from app.billing.plans import PLANS, compute_overage_cents, get_plan
from app.models.usage_record import UsageRecord
from tests.conftest import seed_practice


# ── plan catalog (ADM7 grid) ─────────────────────────────────────────────────
def test_plan_prices_match_spec():
    # Prices match the public site (canonical): $249/399/599/899.
    assert PLANS["after_hours"].monthly_price_cents == 24900
    assert PLANS["after_hours"].included_minutes == 1500
    assert PLANS["after_hours"].overage_cents_per_min == 18
    assert PLANS["full_time"].monthly_price_cents == 39900
    assert PLANS["full_time"].included_minutes == 2500
    assert PLANS["full_time"].overage_cents_per_min == 15
    assert PLANS["growth"].monthly_price_cents == 59900
    assert PLANS["growth"].included_minutes == 4000
    assert PLANS["multi"].monthly_price_cents == 89900
    assert PLANS["multi"].included_minutes == 3000
    assert PLANS["multi"].overage_cents_per_min == 11
    assert PLANS["multi"].per_location is True
    assert set(PLANS) == {"after_hours", "full_time", "growth", "multi"}


def test_annual_discount_15pct():
    p = PLANS["full_time"]
    # 399*12 = 4788.00 → 15% off → 407us... round(399_00*12*0.85) cents.
    assert p.annual_total_cents == round(39900 * 12 * 0.85)


def test_legacy_plan_keys_resolve_to_current_tier():
    # Old checkout links / existing subscription rows must not crash.
    assert get_plan("starter").key == "after_hours"
    assert get_plan("practice").key == "full_time"
    assert get_plan("group").key == "multi"
    assert get_plan("nope") is None


def test_overage_ceils_partial_minutes():
    # 1000 included, used 1002.1 → 3 overage minutes (ceil) * 25c = 75c
    assert compute_overage_cents(1002.1, 1000, 25) == 75
    # under the limit → no overage
    assert compute_overage_cents(900, 1000, 25) == 0
    assert get_plan("nope") is None


def test_month_bounds_wraps_december():
    s, e = _month_bounds(datetime(2026, 12, 14, 10, tzinfo=UTC))
    assert s == datetime(2026, 12, 1, tzinfo=UTC)
    assert e == datetime(2027, 1, 1, tzinfo=UTC)


# ── metering ─────────────────────────────────────────────────────────────────
async def test_record_call_usage_accumulates(db_session):
    practice, _ = await seed_practice(
        db_session, name="Meter", clerk_org_id="org_m", clerk_user_id="u_m"
    )
    now = datetime(2026, 6, 15, 12, tzinfo=UTC)
    await record_call_usage(db_session, practice.id, 90, now=now)   # 1.5 min
    await record_call_usage(db_session, practice.id, 30, now=now)   # +0.5 min
    await db_session.commit()

    row = (
        await db_session.execute(
            select(UsageRecord).where(UsageRecord.practice_id == practice.id)
        )
    ).scalar_one()
    assert row.minutes_used == Decimal("2.00")
    assert row.calls_count == 2


async def test_record_sms_usage_increments(db_session):
    practice, _ = await seed_practice(
        db_session, name="Sms", clerk_org_id="org_s", clerk_user_id="u_s"
    )
    now = datetime(2026, 6, 15, 12, tzinfo=UTC)
    await record_sms_usage(db_session, practice.id, now=now)
    await record_sms_usage(db_session, practice.id, now=now)
    await db_session.commit()
    row = (
        await db_session.execute(
            select(UsageRecord).where(UsageRecord.practice_id == practice.id)
        )
    ).scalar_one()
    assert row.sms_count == 2
