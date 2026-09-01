"""Phase D — plan catalog math + usage metering."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.billing.metering import _month_bounds, record_call_usage, record_sms_usage
from app.billing.plans import PLANS, compute_overage_cents, get_plan
from app.models.usage_record import UsageRecord
from app.observability import alerts
from tests.conftest import seed_practice


# ── plan catalog ─────────────────────────────────────────────────────────────
def test_plan_prices_match_spec():
    assert PLANS["overflow"].monthly_price_cents == 29900
    assert PLANS["overflow"].included_minutes == 400
    assert PLANS["front_desk"].monthly_price_cents == 49900
    assert PLANS["front_desk"].included_minutes == 650
    assert PLANS["revenue"].monthly_price_cents == 74900
    assert PLANS["revenue"].included_minutes == 1000
    assert PLANS["multi"].monthly_price_cents == 64900
    assert PLANS["multi"].included_minutes == 900
    assert PLANS["multi"].per_location is True
    assert set(PLANS) == {"overflow", "front_desk", "revenue", "multi"}


def test_every_allowance_holds_the_target_margin_at_full_use():
    """The property the old grid did not have.

    Its allowances implied 15.0-16.6c of revenue per included minute against a
    14.8c measured voice floor — chosen by feel, with the arithmetic never done.
    A clinic that used everything it paid for was our worst customer.

    Each allowance must now be no larger than the margin target permits, so the
    heaviest legitimate user is still profitable."""
    from app.billing.plans import (
        PRICING_COST_CENTS_PER_MIN,
        TARGET_GROSS_MARGIN,
        included_minutes_for,
    )

    for plan in PLANS.values():
        ceiling = included_minutes_for(plan.monthly_price_cents)
        assert plan.included_minutes <= ceiling, (
            f"{plan.key}: {plan.included_minutes} min exceeds the {ceiling} the "
            f"price can carry at {TARGET_GROSS_MARGIN:.0%}"
        )
        gm_at_cap = 1 - (
            plan.included_minutes * PRICING_COST_CENTS_PER_MIN
        ) / plan.monthly_price_cents
        assert gm_at_cap >= TARGET_GROSS_MARGIN, f"{plan.key}: {gm_at_cap:.1%} at cap"


def test_no_plan_sells_an_extra_minute_below_cost():
    """Three of the four old rates were under the MEASURED voice floor, before
    any PMS cost — and the rate fell as the tier rose, so a clinic growing into
    a bigger plan cost us more the more it used us."""
    from app.billing.plans import PRICING_COST_CENTS_PER_MIN

    rates = {p.overage_cents_per_min for p in PLANS.values()}
    assert len(rates) == 1, f"overage must be one rate for every tier, found {rates}"
    assert rates.pop() > PRICING_COST_CENTS_PER_MIN


def test_the_annual_discount_does_not_eat_the_margin():
    """A discount is a margin decision, not a habit. At 15% off a 65% target the
    heaviest annual customer fell under 60%; 10% keeps every tier above it."""
    from app.billing.plans import ANNUAL_DISCOUNT, PRICING_COST_CENTS_PER_MIN

    assert ANNUAL_DISCOUNT == 0.10
    for plan in PLANS.values():
        monthly_equiv = plan.annual_total_cents / 12
        gm = 1 - (plan.included_minutes * PRICING_COST_CENTS_PER_MIN) / monthly_equiv
        assert gm >= 0.60, f"{plan.key}: {gm:.1%} on annual at full use"


def test_legacy_plan_keys_resolve_to_current_tier():
    """Old checkout links and demo rows must not crash. The 2026-08 keys are here
    for the same reason — no practice was on a paid plan when the grid changed,
    so these are for stale links, not a migration."""
    assert get_plan("starter").key == "overflow"
    assert get_plan("practice").key == "front_desk"
    assert get_plan("group").key == "multi"
    assert get_plan("after_hours").key == "overflow"
    assert get_plan("full_time").key == "front_desk"
    assert get_plan("growth").key == "revenue"
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


# ── the moment the plan runs out ─────────────────────────────────────────────
#
# Passing the included minutes breaks nothing. Calls keep being answered, the
# dashboard looks normal, and the first sign is the invoice — for us a margin
# we did not price, for the clinic a bill nobody warned them about.


async def _subscribed(db_session, practice_id, included_minutes):
    from app.models.subscription import Subscription

    db_session.add(Subscription(
        id=uuid.uuid4(), practice_id=practice_id, plan="overflow",
        status="active", included_minutes=included_minutes,
    ))
    await db_session.flush()


async def test_passing_the_included_minutes_raises_one_alert(db_session):
    practice, _ = await seed_practice(
        db_session, name="Cap", clerk_org_id="org_cap1", clerk_user_id="u_cap1"
    )
    await _subscribed(db_session, practice.id, included_minutes=2)
    now = datetime(2026, 6, 15, 12, tzinfo=UTC)

    alerts._RECENT.clear()
    await record_call_usage(db_session, practice.id, 60, now=now)   # 1 min, under
    assert alerts.recent_alerts()["count_last_hour"] == 0

    await record_call_usage(db_session, practice.id, 90, now=now)   # 2.5 min, over
    fired = alerts.recent_alerts()
    assert fired["by_kind"] == {"usage_cap_exceeded": 1}

    # Every later call is also over the cap. Alerting on each would make the
    # signal worthless by the end of the day; the crossing happens once.
    await record_call_usage(db_session, practice.id, 600, now=now)
    assert alerts.recent_alerts()["by_kind"] == {"usage_cap_exceeded": 1}


async def test_a_practice_with_no_subscription_is_not_alerted(db_session):
    """The pilot clinics have no plan. Nothing to exceed, nothing to say."""
    practice, _ = await seed_practice(
        db_session, name="Cap2", clerk_org_id="org_cap2", clerk_user_id="u_cap2"
    )
    alerts._RECENT.clear()
    await record_call_usage(
        db_session, practice.id, 60_000, now=datetime(2026, 6, 15, 12, tzinfo=UTC)
    )
    assert alerts.recent_alerts()["count_last_hour"] == 0
