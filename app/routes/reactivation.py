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


# ---------------------------------------------------------------------------
# Custom campaigns (clinic-authored, 2026-07): upload contacts (CSV text /
# pasted numbers / a single number), write your own message, launch.
# Mutations = MANAGE_SETTINGS (owner/manager) — campaign sends are a practice-
# level decision. Marketing category REQUIRES the written-consent attestation
# checkbox; the attesting user + timestamp are stored on the campaign (PEWC
# warranty, see LEGAL_COMPLIANCE_US §2/§6).
# ---------------------------------------------------------------------------
from datetime import UTC, datetime  # noqa: E402 — section imports
from typing import Literal  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.auth.permissions import MANAGE_SETTINGS, require_permission  # noqa: E402
from app.models.reactivation import (  # noqa: E402
    ReactivationCampaign,
    ReactivationTarget,
)
from app.models.user import User  # noqa: E402
from app.services.reactivation.campaign import (  # noqa: E402
    CAMPAIGN_CATEGORIES,
    CAMPAIGN_CHANNELS,
    build_custom_campaign,
    launch_campaign,
)
from app.services.reactivation.contacts import parse_contacts, upsert_contacts  # noqa: E402


class ContactsUpload(BaseModel):
    # CSV text, newline-separated numbers, or a single number — one field.
    contacts_text: str


class CustomCampaignCreate(BaseModel):
    name: str
    contacts_text: str
    custom_context: str | None = None
    category: Literal["treatment", "marketing"] = "treatment"
    channels: Literal["sms", "voice", "both"] = "sms"
    # Marketing only: the clinic affirms it holds prior express WRITTEN consent
    # for every uploaded contact. Required checkbox in the UI.
    consent_attested: bool = False
    launch_now: bool = False


class CampaignRow(BaseModel):
    id: str
    name: str
    status: str
    category: str
    channels: str
    custom_context: str | None
    enrolled: int
    contacted: int
    booked: int
    opted_out: int
    created_at: datetime


class CampaignCreateResponse(BaseModel):
    campaign: CampaignRow
    intake: dict  # added/updated/invalid_count/invalid_samples/duplicates_in_file


async def _campaign_row(db: AsyncSession, c: ReactivationCampaign) -> CampaignRow:
    # Re-bind the tenant: SET LOCAL is wiped by any commit inside the service
    # layer (build/launch), and these counts hit RLS-FORCE tables.
    from app.db import set_tenant
    await set_tenant(db, c.practice_id)
    counts = dict((await db.execute(
        select(ReactivationTarget.status, func.count())
        .where(ReactivationTarget.campaign_id == c.id)
        .group_by(ReactivationTarget.status)
    )).all())
    enrolled = sum(counts.values())
    contacted = enrolled - counts.get("pending", 0)
    return CampaignRow(
        id=str(c.id), name=c.name, status=c.status, category=c.category,
        channels=c.channels, custom_context=c.custom_context,
        enrolled=enrolled, contacted=contacted,
        booked=counts.get("booked", 0), opted_out=counts.get("opted_out", 0),
        created_at=c.created_at,
    )


@router.post("/contacts", response_model=dict)
async def upload_contacts(
    payload: ContactsUpload,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> dict:
    """Upload patient contacts without creating a campaign (grow the base)."""
    rows, report = parse_contacts(payload.contacts_text)
    if not rows and report.invalid:
        raise HTTPException(status_code=422,
                            detail="No valid US phone numbers found in the upload.")
    _, report = await upsert_contacts(db, practice.id, rows, report)
    await db.commit()
    return report.as_dict()


@router.post("/campaigns", response_model=CampaignCreateResponse)
async def create_custom_campaign(
    payload: CustomCampaignCreate,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> CampaignCreateResponse:
    """Upload contacts + create a campaign in one shot (optionally launch)."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Campaign name is required.")
    if payload.category not in CAMPAIGN_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown category.")
    if payload.channels not in CAMPAIGN_CHANNELS:
        raise HTTPException(status_code=422, detail="Unknown channels value.")
    if payload.category == "marketing":
        if not payload.consent_attested:
            raise HTTPException(
                status_code=422,
                detail="Marketing campaigns require the written-consent "
                       "attestation (TCPA prior express written consent).",
            )
        if not (payload.custom_context or "").strip():
            raise HTTPException(status_code=422,
                                detail="Marketing campaigns need your message text.")

    rows, report = parse_contacts(payload.contacts_text)
    if not rows:
        raise HTTPException(status_code=422,
                            detail="No valid US phone numbers found in the upload.")
    patient_ids, report = await upsert_contacts(db, practice.id, rows, report)
    await db.commit()

    campaign = await build_custom_campaign(
        db, practice.id, name=name, patient_ids=patient_ids,
        category=payload.category, custom_context=payload.custom_context,
        channels=payload.channels, created_by_user_id=user.id,
        consent_attested_by=(user.id if payload.category == "marketing" else None),
    )
    if payload.launch_now:
        await launch_campaign(db, practice.id, campaign.id)
        campaign.status = "running"

    return CampaignCreateResponse(
        campaign=await _campaign_row(db, campaign), intake=report.as_dict(),
    )


@router.get("/campaigns", response_model=list[CampaignRow])
async def list_campaigns(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> list[CampaignRow]:
    rows = (await db.execute(
        select(ReactivationCampaign)
        .where(ReactivationCampaign.practice_id == practice.id)
        .order_by(ReactivationCampaign.created_at.desc()).limit(100)
    )).scalars().all()
    return [await _campaign_row(db, c) for c in rows]


@router.post("/campaigns/{campaign_id}/launch", response_model=CampaignRow)
async def launch_custom_campaign(
    campaign_id: uuid.UUID,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> CampaignRow:
    c = (await db.execute(
        select(ReactivationCampaign).where(
            ReactivationCampaign.id == campaign_id,
            ReactivationCampaign.practice_id == practice.id,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    if c.status not in ("draft", "paused"):
        raise HTTPException(status_code=409, detail=f"Campaign is {c.status}.")
    c.status = "running"
    c.started_at = c.started_at or datetime.now(tz=UTC)
    await db.commit()
    return await _campaign_row(db, c)


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignRow)
async def pause_custom_campaign(
    campaign_id: uuid.UUID,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> CampaignRow:
    c = (await db.execute(
        select(ReactivationCampaign).where(
            ReactivationCampaign.id == campaign_id,
            ReactivationCampaign.practice_id == practice.id,
        )
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    if c.status != "running":
        raise HTTPException(status_code=409, detail=f"Campaign is {c.status}.")
    c.status = "paused"
    await db.commit()
    return await _campaign_row(db, c)
