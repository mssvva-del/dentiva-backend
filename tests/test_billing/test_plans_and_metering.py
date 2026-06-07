"""Phase D — plan catalog math + usage metering."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.billing.metering import _month_bounds, record_call_usage, record_sms_usage
from app.billing.plans import PLANS, compute_overage_cents, get_plan
from app.models.usage_record import UsageRecord
from tests.conftest import seed_practice


# ── plan catalog ─────────────────────────────────────────────────────────────
def test_plan_prices_match_spec():
    assert PLANS["starter"].monthly_price_cents == 14900
    assert PLANS["starter"].included_minutes == 500
    assert PLANS["starter"].overage_cents_per_min == 30
    assert PLANS["practice"].monthly_price_cents == 34900
    assert PLANS["practice"].included_minutes == 1000
    assert PLANS["practice"].overage_cents_per_min == 25
    assert PLANS["group"].monthly_price_cents == 59900
    assert PLANS["group"].included_minutes == 2000
    assert PLANS["group"].overage_cents_per_min == 20
    assert PLANS["group"].per_location is True


def test_annual_discount_17pct():
    p = PLANS["practice"]
    # 349*12 = 4188.00 → 17% off = 3476.04 → 347604 cents
    assert p.annual_total_cents == round(34900 * 12 * 0.83)


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
