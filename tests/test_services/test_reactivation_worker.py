"""Reactivation worker loop (the queue) — multi-practice tick."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

import app.db as app_db
from app.adapters.nexhealth.mock import MockReactivationSource
from app.db import set_tenant
from app.models.reactivation import ReactivationTouch
from app.services.reactivation.campaign import build_campaign, launch_campaign
from app.services.reactivation.scheduling import CadenceStep, CampaignConfig
from app.services.reactivation.segmentation import LAPSED
from app.services.reactivation.worker import run_reactivation_tick
from tests.conftest import seed_practice

# In-window so the first touch is due at _NOW.
_NOW = datetime(2026, 6, 25, 16, 0, tzinfo=UTC)
_CFG = CampaignConfig(
    cadence=(CadenceStep("sms", 0),), quiet_start_hour=0, quiet_end_hour=24
)


async def _records():
    return await MockReactivationSource().pull_reactivation_records()


async def _touch_count(practice_id):
    async with app_db.async_session_factory() as s:
        await set_tenant(s, practice_id)
        return (await s.execute(
            select(func.count()).select_from(ReactivationTouch)
            .where(ReactivationTouch.practice_id == practice_id)
        )).scalar_one()


async def test_tick_drains_due_touches_across_practices(db_session):
    # Two practices, each with a launched campaign.
    pa, _ = await seed_practice(
        db_session, name="WK_A", clerk_org_id="o_wka", clerk_user_id="u_wka")
    pb, _ = await seed_practice(
        db_session, name="WK_B", clerk_org_id="o_wkb", clerk_user_id="u_wkb")
    for p in (pa, pb):
        camp = await build_campaign(db_session, p.id, LAPSED, await _records(),
                                    now=_NOW, campaign_config=_CFG)
        await launch_campaign(db_session, p.id, camp.id, now=_NOW)

    totals = await run_reactivation_tick(app_db.async_session_factory, now=_NOW)
    assert totals["processed"] >= 2  # at least one target per practice
    assert await _touch_count(pa.id) >= 1
    assert await _touch_count(pb.id) >= 1


async def test_tick_noop_for_draft_campaign(db_session):
    # Built but NOT launched (draft) → nothing due → no touches.
    p, _ = await seed_practice(db_session, name="WK_C", clerk_org_id="o_wkc", clerk_user_id="u_wkc")
    await build_campaign(db_session, p.id, LAPSED, await _records(), now=_NOW, campaign_config=_CFG)

    totals = await run_reactivation_tick(app_db.async_session_factory, now=_NOW)
    assert await _touch_count(p.id) == 0
    assert isinstance(totals["processed"], int)
