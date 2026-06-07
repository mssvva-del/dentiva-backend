"""Billing lifecycle service (Platform Iter 1, Phase D).

Owns subscription creation, suspension, and reconciliation from Stripe events.
Our DB row is authoritative for entitlement, so this works before Stripe is wired
and lets super_admin run pilots/custom deals without a Stripe object.

Money is cents (ints). The subscription snapshots included_minutes / mrr / overage
at creation so a later catalog price change doesn't silently re-price live clients.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.plans import get_plan
from app.models.practice import Practice
from app.models.subscription import Subscription


async def get_subscription(
    session: AsyncSession, practice_id: uuid.UUID
) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(Subscription.practice_id == practice_id)
        )
    ).scalar_one_or_none()


async def create_or_update_subscription(
    session: AsyncSession,
    practice: Practice,
    *,
    plan_key: str,
    billing_cycle: str = "monthly",
    status: str = "active",
    stripe_subscription_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> Subscription:
    """Create (or update) the practice's single subscription from a plan.

    Entitlement (included_minutes, mrr, overage) is taken from the catalog but
    stored on the row so it can be overridden per client later by super_admin.
    """
    plan = get_plan(plan_key)
    if plan is None:
        raise ValueError(f"unknown plan '{plan_key}'")

    mrr = (
        plan.annual_monthly_equivalent_cents
        if billing_cycle == "annual"
        else plan.monthly_price_cents
    )

    sub = await get_subscription(session, practice.id)
    if sub is None:
        sub = Subscription(id=uuid.uuid4(), practice_id=practice.id, plan=plan_key)
        session.add(sub)

    sub.plan = plan_key
    sub.billing_cycle = billing_cycle
    sub.status = status
    sub.included_minutes = plan.included_minutes
    sub.mrr_cents = mrr
    sub.overage_cents_per_min = plan.overage_cents_per_min
    if stripe_subscription_id:
        sub.stripe_subscription_id = stripe_subscription_id
    if period_start:
        sub.current_period_start = period_start
    if period_end:
        sub.current_period_end = period_end
    return sub


async def set_pilot(
    session: AsyncSession, practice: Practice, *, setup_fee_cents: int | None = None
) -> Subscription:
    """Put a practice on a manual PILOT (super_admin concierge deal).

    No recurring revenue is assumed (mrr 0); an optional one-time setup fee is
    recorded. The practice itself is marked 'pilot' so service stays enabled.
    """
    sub = await get_subscription(session, practice.id)
    if sub is None:
        # Pilots default to Starter entitlement so usage limits/UX still work.
        sub = await create_or_update_subscription(
            session, practice, plan_key="starter", status="pilot"
        )
    sub.status = "pilot"
    sub.mrr_cents = 0
    sub.setup_fee_cents = setup_fee_cents
    practice.status = "pilot"
    return sub


async def suspend_practice(session: AsyncSession, practice: Practice) -> None:
    """Suspend service (e.g. after the failed-payment grace period elapses).

    Marks both the practice and its subscription suspended. Reversible via
    reactivate_practice once payment is resolved.
    """
    practice.status = "suspended"
    sub = await get_subscription(session, practice.id)
    if sub is not None:
        sub.status = "suspended"


async def reactivate_practice(session: AsyncSession, practice: Practice) -> None:
    """Lift a suspension (payment resolved)."""
    practice.status = "active"
    sub = await get_subscription(session, practice.id)
    if sub is not None and sub.status == "suspended":
        sub.status = "active"


async def mark_past_due(session: AsyncSession, practice_id: uuid.UUID) -> None:
    """Enter the failed-payment grace period (Stripe will retry the charge)."""
    sub = await get_subscription(session, practice_id)
    if sub is not None and sub.status not in ("suspended", "cancelled"):
        sub.status = "past_due"


def _now() -> datetime:
    return datetime.now(UTC)
