"""Public pricing endpoint (ADM6) — the marketing site reads the price grid here.

Unauthenticated + read-only, so the site never hardcodes prices: Sergio edits them
in the admin panel and the change is live without a deploy. Rate-limited per IP.
Returns only PUBLIC, active plans + the invitee/annual discount percents the site
advertises (referrer reward is internal — not exposed).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

import app.db as _app_db
from app.middleware.rate_limit import limit_public
from app.services.pricing import get_active_plans, get_or_create_settings


class PublicPlan(BaseModel):
    key: str
    name: str
    monthly_cents: int
    annual_monthly_cents: int
    soft_cap_minutes: int
    overage_cents_per_min: int
    reactivation_level: str
    per_location: bool
    highlight: bool


class PublicPricing(BaseModel):
    plans: list[PublicPlan]
    annual_discount_percent: int
    invitee_discount_percent: int


router = APIRouter(prefix="/api/pricing", tags=["pricing"])


@router.get("", response_model=PublicPricing)
@limit_public("60/minute")
async def public_pricing(request: Request, response: Response) -> PublicPricing:
    async with _app_db.async_session_factory() as session:
        plans = await get_active_plans(session)
        settings = await get_or_create_settings(session)
        await session.commit()  # persist a self-healed settings row, if created
        return PublicPricing(
            plans=[
                PublicPlan(
                    key=p.plan_key, name=p.name,
                    monthly_cents=p.monthly_cents,
                    annual_monthly_cents=p.annual_monthly_cents,
                    soft_cap_minutes=p.soft_cap_minutes,
                    overage_cents_per_min=p.overage_cents_per_min,
                    reactivation_level=p.reactivation_level,
                    per_location=p.per_location, highlight=p.highlight,
                )
                for p in plans
            ],
            annual_discount_percent=settings.annual_discount_percent,
            invitee_discount_percent=settings.invitee_discount_percent,
        )
