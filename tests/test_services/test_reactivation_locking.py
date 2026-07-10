"""Atomic queue claim — two workers draining the SAME practice must never grab
the SAME target (which would double-text the patient). select_due_targets uses
FOR UPDATE SKIP LOCKED, so a second concurrent reader sees a DISJOINT set (here:
empty, since the only due row is locked by the first reader's open transaction)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import app.db as app_db
from app.models.patient import Patient
from app.models.reactivation import ReactivationCampaign, ReactivationTarget
from app.services.reactivation.campaign import select_due_targets
from tests.conftest import seed_practice


async def _seed_due_target(db_session, practice_id):
    """One running campaign with one due target for the given practice."""
    patient = Patient(id=uuid.uuid4(), practice_id=practice_id,
                      pms_external_id="lock-p", first_name="Lock", phone="+17322840500")
    camp = ReactivationCampaign(id=uuid.uuid4(), practice_id=practice_id,
                                name="lock camp", segment="custom", status="running")
    db_session.add_all([patient, camp])
    await db_session.flush()
    target = ReactivationTarget(
        id=uuid.uuid4(), practice_id=practice_id, campaign_id=camp.id,
        patient_id=patient.id, segment="custom", status="pending",
        next_touch_at=datetime.now(tz=UTC) - timedelta(minutes=1),
    )
    db_session.add(target)
    await db_session.commit()
    return target


async def test_skip_locked_gives_disjoint_batches(client, db_session):
    practice, _ = await seed_practice(db_session, name="LockCo",
                                      clerk_org_id="org_lock", clerk_user_id="user_lock")
    await _seed_due_target(db_session, practice.id)

    # Two independent app sessions (NullPool → separate connections), like two
    # instances draining concurrently. The first holds its transaction OPEN so its
    # FOR UPDATE lock on the target persists; the second must SKIP it.
    async with app_db.async_session_factory() as s1, app_db.async_session_factory() as s2:
        due1 = await select_due_targets(s1, practice.id)
        assert len(due1) == 1  # first worker claims the only due target

        due2 = await select_due_targets(s2, practice.id)
        assert due2 == []  # locked by s1 → skipped, NOT handed out twice
