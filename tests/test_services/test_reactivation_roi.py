"""Reactivation ROI tracking (block 9) — attribution + funnel/revenue."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.nexhealth.mock import MockReactivationSource
from app.db import set_tenant
from app.models.booking import Booking
from app.models.reactivation import ReactivationTarget
from app.services.reactivation.campaign import build_campaign, launch_campaign
from app.services.reactivation.outreach import process_due_targets
from app.services.reactivation.roi import attribute_booking, campaign_roi
from app.services.reactivation.scheduling import CadenceStep, CampaignConfig
from app.services.reactivation.segmentation import LAPSED
from tests.conftest import seed_practice

_CFG = CampaignConfig(
    cadence=(CadenceStep("sms", 0),), quiet_start_hour=0, quiet_end_hour=24
)


async def _records():
    return await MockReactivationSource().pull_reactivation_records()


async def _campaign_with_contacted(db_session, practice):
    """Build + launch + make one SMS touch so targets are 'contacted'."""
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=_CFG
    )
    await launch_campaign(db_session, practice.id, camp.id)
    await process_due_targets(db_session, practice.id, config=_CFG)
    return camp


async def test_roi_funnel_before_any_booking(db_session):
    practice, _ = await seed_practice(
        db_session, name="ROI1", clerk_org_id="org_roi1", clerk_user_id="u_roi1"
    )
    await _campaign_with_contacted(db_session, practice)
    roi = await campaign_roi(db_session, practice.id)
    assert roi["enrolled"] >= 1
    assert roi["contacted"] == roi["enrolled"]   # everyone got the SMS
    assert roi["booked"] == 0
    assert roi["revenue_recovered_cents"] == 0
    assert roi["conversion_rate"] == 0.0


async def test_attribute_booking_then_roi(db_session):
    practice, _ = await seed_practice(
        db_session, name="ROI2", clerk_org_id="org_roi2", clerk_user_id="u_roi2"
    )
    await _campaign_with_contacted(db_session, practice)

    # Pick the top target (nh-1001, highest value) and book it.
    await set_tenant(db_session, practice.id)
    target = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id)
        .order_by(ReactivationTarget.value_score.desc()).limit(1)
    )).scalar_one()
    expected_revenue = int(target.value_score)

    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=target.patient_id,
        appointment_at=target.created_at, duration_minutes=60,
        status="confirmed", source="reactivation",
    )
    db_session.add(booking)
    await db_session.commit()

    ok = await attribute_booking(db_session, practice.id, target.patient_id, booking.id)
    assert ok is True

    roi = await campaign_roi(db_session, practice.id)
    assert roi["booked"] == 1
    assert roi["revenue_recovered_cents"] == expected_revenue
    assert roi["conversion_rate"] == round(1 / roi["contacted"], 4)

    await set_tenant(db_session, practice.id)
    refreshed = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.id == target.id)
    )).scalar_one()
    assert refreshed.status == "booked" and refreshed.booking_id == booking.id


async def test_attribute_booking_no_active_target_returns_false(db_session):
    practice, _ = await seed_practice(
        db_session, name="ROI3", clerk_org_id="org_roi3", clerk_user_id="u_roi3"
    )
    # No campaign — a random patient booking isn't reactivation-driven.
    ok = await attribute_booking(
        db_session, practice.id, uuid.uuid4(), uuid.uuid4()
    )
    assert ok is False
