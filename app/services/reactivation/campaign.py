"""Campaign builder + scheduler (Phase 1, block 5).

Turns a prioritized segment selection into a persisted campaign with enrolled,
scheduled targets — the bridge between "who/what order" (blocks 3-4) and the
outreach worker (blocks 6-7). It does NOT send anything yet.

Build steps (all in one transaction, tenant bound once):
  1. cache the pulled PMS records into our patients table,
  2. prioritize the chosen segment (value scoring),
  3. create the campaign + enroll one target per patient, EXCLUDING:
       - patients who opted out of SMS on OUR side (patient.sms_opt_out), and
       - patients who already have an upcoming confirmed appointment (dedup —
         never recall someone already booked),
     stamping each target's value_score and the first-touch time (quiet-hours
     aware).

The PMS-level opt-out was already dropped during prioritization; this adds OUR
opt-out + the appointment dedup, which need our own DB.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.db import set_tenant
from app.models.booking import Booking
from app.models.practice import Practice
from app.models.reactivation import ReactivationCampaign, ReactivationTarget
from app.services.reactivation.patient_sync import upsert_patients
from app.services.reactivation.scheduling import (
    CampaignConfig,
    first_touch_at,
    next_allowed_time,
)
from app.services.reactivation.scoring import ScoringConfig, prioritize_for_segment
from app.services.reactivation.segmentation import SegmentationConfig

logger = logging.getLogger("dentiva.reactivation")


async def _patients_with_upcoming_booking(
    session: AsyncSession, practice_id: uuid.UUID, patient_ids: list[uuid.UUID], now: datetime
) -> set[uuid.UUID]:
    """Which of these patients already have a confirmed upcoming appointment —
    they're excluded from reactivation (no point recalling the already-booked)."""
    if not patient_ids:
        return set()
    rows = await session.execute(
        select(Booking.patient_id)
        .where(
            Booking.practice_id == practice_id,
            Booking.patient_id.in_(patient_ids),
            Booking.status == "confirmed",
            Booking.appointment_at >= now,
        )
        .distinct()
    )
    return {r[0] for r in rows}


async def build_campaign(
    session: AsyncSession,
    practice_id: uuid.UUID,
    segment: str,
    records: list[PMSReactivationRecord],
    *,
    now: datetime | None = None,
    name: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
    seg_config: SegmentationConfig = SegmentationConfig(),
    score_config: ScoringConfig = ScoringConfig(),
    campaign_config: CampaignConfig = CampaignConfig(),
) -> ReactivationCampaign:
    """Create a 'draft' campaign and enroll its prioritized, scheduled targets."""
    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)

    patients = await upsert_patients(session, practice_id, records, now=now)
    ranked = prioritize_for_segment(
        records, segment, now=now.date(), seg_config=seg_config, score_config=score_config
    )

    practice = (
        await session.execute(select(Practice).where(Practice.id == practice_id))
    ).scalar_one()
    touch_at = first_touch_at(now, practice.timezone, campaign_config)

    ranked_pids = [patients[rec.pms_external_id].id for _, rec in ranked]
    already_booked = await _patients_with_upcoming_booking(
        session, practice_id, ranked_pids, now
    )

    campaign = ReactivationCampaign(
        id=uuid.uuid4(),
        practice_id=practice_id,
        name=name or f"{segment} · {now.date().isoformat()}",
        segment=segment,
        status="draft",
        created_by_user_id=created_by_user_id,
    )
    session.add(campaign)
    await session.flush()

    enrolled = 0
    for score_val, rec in ranked:
        patient = patients[rec.pms_external_id]
        if patient.sms_opt_out:
            continue  # honored our own opt-out
        if patient.id in already_booked:
            continue  # already has an upcoming appointment — don't recall
        session.add(
            ReactivationTarget(
                id=uuid.uuid4(),
                practice_id=practice_id,
                campaign_id=campaign.id,
                patient_id=patient.id,
                segment=segment,
                value_score=Decimal(score_val),
                status="pending",
                next_touch_at=touch_at,
                touches_count=0,
            )
        )
        enrolled += 1

    await session.commit()
    logger.info(
        "reactivation campaign built: segment=%s enrolled=%s (of %s ranked) practice=%s",
        segment, enrolled, len(ranked), practice_id,
    )
    return campaign


async def launch_campaign(
    session: AsyncSession,
    practice_id: uuid.UUID,
    campaign_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> None:
    """Flip a draft campaign to 'running' so the worker starts making touches."""
    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)
    campaign = (
        await session.execute(
            select(ReactivationCampaign).where(ReactivationCampaign.id == campaign_id)
        )
    ).scalar_one()
    campaign.status = "running"
    campaign.started_at = now
    await session.commit()


async def select_due_targets(
    session: AsyncSession,
    practice_id: uuid.UUID,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[ReactivationTarget]:
    """Targets due for a touch right now, highest-value first — what the outreach
    worker (block 6/7) pulls each tick. Only targets in RUNNING campaigns whose
    next_touch_at has passed. Quiet-hours are re-checked by the worker at send."""
    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)
    rows = await session.execute(
        select(ReactivationTarget)
        .join(
            ReactivationCampaign,
            ReactivationTarget.campaign_id == ReactivationCampaign.id,
        )
        .where(
            ReactivationCampaign.status == "running",
            ReactivationTarget.practice_id == practice_id,
            ReactivationTarget.status.in_(("pending", "in_progress")),
            ReactivationTarget.next_touch_at <= now,
        )
        .order_by(ReactivationTarget.value_score.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Custom clinic-authored campaigns (2026-07): the clinic uploads a contact list
# (or picks all patients) and writes its own message/direction. Unlike the
# PMS-driven build_campaign above there is no segmentation math — the clinic
# chose the audience; every enrolled patient just enters the cadence queue.
# ---------------------------------------------------------------------------

CUSTOM_SEGMENT = "custom"
CAMPAIGN_CATEGORIES = ("treatment", "marketing")
CAMPAIGN_CHANNELS = ("sms", "voice", "both")


async def build_custom_campaign(
    session: AsyncSession,
    practice_id: uuid.UUID,
    *,
    name: str,
    patient_ids: list[uuid.UUID],
    category: str = "treatment",
    custom_context: str | None = None,
    channels: str = "sms",
    created_by_user_id: uuid.UUID | None = None,
    consent_attested_by: uuid.UUID | None = None,
    now: datetime | None = None,
    campaign_config: CampaignConfig = CampaignConfig(),
) -> ReactivationCampaign:
    """Create a DRAFT custom campaign enrolling exactly the given patients.

    Marketing campaigns REQUIRE consent_attested_by (the clinic user affirming
    written consent) — enforced here so no code path can create an unattested
    promo campaign. Patients who opted out of SMS are skipped at enroll time
    (and re-checked at send time by the worker)."""
    if category not in CAMPAIGN_CATEGORIES:
        raise ValueError(f"category must be one of {CAMPAIGN_CATEGORIES}")
    if channels not in CAMPAIGN_CHANNELS:
        raise ValueError(f"channels must be one of {CAMPAIGN_CHANNELS}")
    if category == "marketing" and consent_attested_by is None:
        raise ValueError("marketing campaigns require a written-consent attestation")

    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)

    campaign = ReactivationCampaign(
        id=uuid.uuid4(), practice_id=practice_id, name=name,
        segment=CUSTOM_SEGMENT, status="draft",
        created_by_user_id=created_by_user_id,
        category=category, custom_context=(custom_context or None),
        channels=channels,
        consent_attested_by=consent_attested_by,
        consent_attested_at=now if consent_attested_by else None,
    )
    session.add(campaign)
    await session.flush()

    from app.models.patient import Patient
    first_touch = next_allowed_time(
        now, None, campaign_config.quiet_start_hour, campaign_config.quiet_end_hour
    )
    enrolled = 0
    for pid in dict.fromkeys(patient_ids):  # de-dup, keep order
        patient = (await session.execute(
            select(Patient).where(Patient.id == pid,
                                  Patient.practice_id == practice_id)
        )).scalar_one_or_none()
        if patient is None or patient.sms_opt_out:
            continue
        session.add(ReactivationTarget(
            id=uuid.uuid4(), practice_id=practice_id, campaign_id=campaign.id,
            patient_id=patient.id, segment=CUSTOM_SEGMENT,
            value_score=Decimal("0"), status="pending",
            next_touch_at=first_touch,
        ))
        enrolled += 1
    await session.commit()
    logger.info("custom campaign built practice=%s campaign=%s enrolled=%d",
                practice_id, campaign.id, enrolled)
    return campaign
