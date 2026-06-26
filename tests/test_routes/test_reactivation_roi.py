"""GET /api/reactivation/roi — the reactivation ROI screen endpoint."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.nexhealth.mock import MockReactivationSource
from app.db import set_tenant
from app.models.booking import Booking
from app.models.reactivation import ReactivationTarget
from app.services.reactivation.campaign import build_campaign, launch_campaign
from app.services.reactivation.outreach import process_due_targets
from app.services.reactivation.roi import attribute_booking
from app.services.reactivation.scheduling import CadenceStep, CampaignConfig
from app.services.reactivation.segmentation import LAPSED
from tests.conftest import seed_practice

_CFG = CampaignConfig(
    cadence=(CadenceStep("sms", 0),), quiet_start_hour=0, quiet_end_hour=24
)


def _hdr(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _records():
    return await MockReactivationSource().pull_reactivation_records()


async def test_roi_requires_auth(client):
    resp = await client.get("/api/reactivation/roi")
    assert resp.status_code == 401


async def test_roi_reports_funnel_and_revenue(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="ROI Co", clerk_org_id="org_roisc", clerk_user_id="u_roisc"
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=_CFG
    )
    await launch_campaign(db_session, practice.id, camp.id)
    await process_due_targets(db_session, practice.id, config=_CFG)

    # Book the top target → recovered revenue.
    await set_tenant(db_session, practice.id)
    target = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id)
        .order_by(ReactivationTarget.value_score.desc()).limit(1)
    )).scalar_one()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=target.patient_id,
        appointment_at=target.created_at, duration_minutes=60,
        status="confirmed", source="reactivation",
    )
    db_session.add(booking)
    await db_session.commit()
    await attribute_booking(db_session, practice.id, target.patient_id, booking.id)

    resp = await client.get("/api/reactivation/roi", headers=_hdr("org_roisc", "u_roisc"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolled"] >= 1
    assert body["contacted"] == body["enrolled"]
    assert body["booked"] == 1
    assert body["revenue_recovered_cents"] == int(target.value_score)
    assert body["revenue_recovered_dollars"] == round(int(target.value_score) / 100, 2)
    assert 0 < body["conversion_rate"] <= 1


async def test_roi_empty_practice_zeroes(client, db_session):
    await seed_practice(
        db_session, name="Empty", clerk_org_id="org_empty", clerk_user_id="u_empty"
    )
    resp = await client.get("/api/reactivation/roi", headers=_hdr("org_empty", "u_empty"))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "enrolled": 0, "contacted": 0, "booked": 0, "no_answer": 0, "opted_out": 0,
        "revenue_recovered_cents": 0, "revenue_recovered_dollars": 0.0,
        "conversion_rate": 0.0,
    }
