"""Reactivation campaign builder + scheduler (block 5) — DB-backed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.adapters.nexhealth.mock import MockReactivationSource
from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.reactivation import ReactivationTarget
from app.services.reactivation.campaign import (
    build_campaign,
    launch_campaign,
    select_due_targets,
)
from app.services.reactivation.segmentation import LAPSED
from tests.conftest import seed_practice

# Within NY business hours (12:00 EDT) so the first touch is due immediately.
_NOW = datetime(2026, 6, 25, 16, 0, tzinfo=UTC)


async def _records():
    return await MockReactivationSource().pull_reactivation_records()


async def _targets(db_session, practice):
    await set_tenant(db_session, practice.id)
    rows = await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id)
    )
    return list(rows.scalars().all())


async def test_build_campaign_enrolls_prioritized_lapsed(db_session):
    practice, _ = await seed_practice(
        db_session, name="RC", clerk_org_id="org_rc1", clerk_user_id="u_rc1"
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), now=_NOW
    )
    assert camp.segment == LAPSED and camp.status == "draft"

    tgts = await _targets(db_session, practice)
    # nh-1001 (lapsed + $1800 treatment) enrolled and top value_score.
    by_score = sorted(tgts, key=lambda t: t.value_score, reverse=True)
    top_patient = (
        await db_session.execute(select(Patient).where(Patient.id == by_score[0].patient_id))
    ).scalar_one()
    assert top_patient.pms_external_id == "nh-1001"
    # nh-1004 is lapsed but PMS-opted-out → never enrolled.
    enrolled_ext = {
        (await db_session.execute(select(Patient).where(Patient.id == t.patient_id)))
        .scalar_one().pms_external_id
        for t in tgts
    }
    assert "nh-1004" not in enrolled_ext
    assert all(t.next_touch_at is not None and t.status == "pending" for t in tgts)


async def test_excludes_our_own_sms_opt_out(db_session):
    practice, _ = await seed_practice(
        db_session, name="RC2", clerk_org_id="org_rc2", clerk_user_id="u_rc2"
    )
    # Pre-seed nh-1001 locally with OUR sms_opt_out set — build must skip it.
    await set_tenant(db_session, practice.id)
    db_session.add(Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="nh-1001",
        first_name="Maria", phone="+15551110001", sms_opt_out=True,
    ))
    await db_session.commit()

    await build_campaign(db_session, practice.id, LAPSED, await _records(), now=_NOW)
    enrolled_ext = {
        (await db_session.execute(select(Patient).where(Patient.id == t.patient_id)))
        .scalar_one().pms_external_id
        for t in await _targets(db_session, practice)
    }
    assert "nh-1001" not in enrolled_ext  # excluded by our opt-out


async def test_dedup_patient_with_upcoming_appointment(db_session):
    practice, _ = await seed_practice(
        db_session, name="RC3", clerk_org_id="org_rc3", clerk_user_id="u_rc3"
    )
    await set_tenant(db_session, practice.id)
    pat = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="nh-1001",
        first_name="Maria", phone="+15551110001",
    )
    db_session.add(pat)
    await db_session.flush()
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=pat.id,
        appointment_at=_NOW + timedelta(days=5), duration_minutes=60,
        status="confirmed", source="ai_call",
    ))
    await db_session.commit()

    await build_campaign(db_session, practice.id, LAPSED, await _records(), now=_NOW)
    enrolled_ext = {
        (await db_session.execute(select(Patient).where(Patient.id == t.patient_id)))
        .scalar_one().pms_external_id
        for t in await _targets(db_session, practice)
    }
    assert "nh-1001" not in enrolled_ext  # already booked → not recalled


async def test_first_touch_respects_quiet_hours(db_session):
    practice, _ = await seed_practice(
        db_session, name="RC4", clerk_org_id="org_rc4", clerk_user_id="u_rc4"
    )  # seed default tz = America/New_York
    # 06:00 UTC = 02:00 EDT — before the 08:00 window. Touch must push to 08:00 local.
    early = datetime(2026, 6, 25, 6, 0, tzinfo=UTC)
    await build_campaign(db_session, practice.id, LAPSED, await _records(), now=early)
    tgts = await _targets(db_session, practice)
    ny = ZoneInfo("America/New_York")
    for t in tgts:
        local_hour = t.next_touch_at.astimezone(ny).hour
        assert 8 <= local_hour < 21, f"touch at quiet hour {local_hour}"


async def test_launch_and_select_due_targets(db_session):
    practice, _ = await seed_practice(
        db_session, name="RC5", clerk_org_id="org_rc5", clerk_user_id="u_rc5"
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), now=_NOW
    )
    # Draft campaign → nothing due yet.
    assert await select_due_targets(db_session, practice.id, now=_NOW) == []
    # Launch → targets become due (touch_at == now, in-window).
    await launch_campaign(db_session, practice.id, camp.id, now=_NOW)
    due = await select_due_targets(db_session, practice.id, now=_NOW)
    assert due
    # Ordered highest-value first.
    scores = [t.value_score for t in due]
    assert scores == sorted(scores, reverse=True)
