"""Pricing/config service (ADM6).

Reads the editable MARKETING price grid (pricing_plans) and the singleton business
knobs (platform_settings). Shared by the public GET /api/pricing (site) and the
admin edit endpoints. These tables are seeded by migration, so reads normally find
rows; get_or_create_settings self-heals a missing singleton so the API never 500s
on a fresh/partial DB.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pricing_plan import PlatformSetting, PricingPlan

# The platform_settings row is a SINGLETON pinned to a fixed id. Using one constant
# id (not a random uuid) makes get_or_create race-safe: concurrent callers all target
# the SAME primary key, so an INSERT ... ON CONFLICT DO NOTHING lets exactly one win
# and everyone reads back the same row — no duplicate/orphan singleton rows.
SETTINGS_SINGLETON_ID = uuid.UUID(int=0)


async def get_active_plans(session: AsyncSession) -> list[PricingPlan]:
    """Active plans, cheapest-first (sort_order) — what the public site shows."""
    return list((await session.execute(
        select(PricingPlan).where(PricingPlan.is_active.is_(True))
        .order_by(PricingPlan.sort_order, PricingPlan.monthly_cents)
    )).scalars().all())


async def get_all_plans(session: AsyncSession) -> list[PricingPlan]:
    """Every plan incl. inactive — the admin editor needs to see/toggle all."""
    return list((await session.execute(
        select(PricingPlan).order_by(PricingPlan.sort_order, PricingPlan.monthly_cents)
    )).scalars().all())


async def get_or_create_settings(session: AsyncSession) -> PlatformSetting:
    """The singleton settings row, pinned to SETTINGS_SINGLETON_ID; self-heals if
    absent. Race-safe: the insert targets a fixed PK with ON CONFLICT DO NOTHING, so
    two concurrent callers can never create two rows — one wins, both read it back."""
    row = (await session.execute(
        select(PlatformSetting).where(PlatformSetting.id == SETTINGS_SINGLETON_ID)
    )).scalar_one_or_none()
    if row is not None:
        return row
    # Self-heal (only ever fires on a DB that missed the seed migration). DEFAULTS
    # come from the column server_defaults so this stays in sync with the schema.
    await session.execute(
        pg_insert(PlatformSetting)
        .values(id=SETTINGS_SINGLETON_ID)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.flush()
    return (await session.execute(
        select(PlatformSetting).where(PlatformSetting.id == SETTINGS_SINGLETON_ID)
    )).scalar_one()
