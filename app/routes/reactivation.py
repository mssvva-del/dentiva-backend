"""Reactivation Engine routes — the ROI screen (the product's headline number).

Surfaces the campaign funnel + recovered revenue computed in
``services.reactivation.roi`` (block 9). Read-only, tenant-scoped. Distinct from
``/api/dashboard/roi`` (which is the general "revenue protected by AI bookings"
metric) — this one is specifically the dormant-patient reactivation program.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.practice import Practice
from app.services.reactivation.roi import campaign_roi

router = APIRouter(prefix="/api/reactivation", tags=["reactivation"])


class ReactivationRoiResponse(BaseModel):
    """The reactivation funnel + recovered revenue (per practice, or one campaign)."""
    enrolled: int          # dormant patients enrolled in a campaign
    contacted: int         # received at least one touch
    booked: int            # came back and booked
    no_answer: int
    opted_out: int
    revenue_recovered_cents: int
    revenue_recovered_dollars: float
    conversion_rate: float  # booked / contacted


@router.get("/roi", response_model=ReactivationRoiResponse)
async def reactivation_roi(
    campaign_id: uuid.UUID | None = Query(
        default=None, description="Scope to one campaign; omit for all campaigns."
    ),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> ReactivationRoiResponse:
    """Reactivation ROI for the current practice (the demo/sales headline)."""
    roi = await campaign_roi(db, practice.id, campaign_id=campaign_id)
    return ReactivationRoiResponse(**roi)
