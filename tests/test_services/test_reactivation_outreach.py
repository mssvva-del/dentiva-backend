"""Outbound SMS orchestration (block 6) — copy, result mapping, worker tick."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.adapters.nexhealth.mock import MockReactivationSource
from app.db import set_tenant
from app.models.patient import Patient
from app.models.reactivation import ReactivationTarget, ReactivationTouch
from app.services.reactivation import outreach as outreach_mod
from app.services.reactivation.campaign import build_campaign, launch_campaign
from app.services.reactivation.messages import reactivation_sms_body
from app.services.reactivation.outreach import (
    process_due_targets,
    send_reactivation_sms,
)
from app.services.reactivation.scheduling import CadenceStep, CampaignConfig
from app.services.reactivation.segmentation import (
    DROPPED_TREATMENT,
    LAPSED,
    OVERDUE_RECALL,
)
from tests.conftest import seed_practice

# Always-open window + zero delays so the worker tick is deterministic regardless
# of wall-clock time (the schema's created_at is the real server clock).
_CFG_2SMS = CampaignConfig(
    cadence=(CadenceStep("sms", 0), CadenceStep("sms", 0)),
    quiet_start_hour=0, quiet_end_hour=24,
)


async def _records():
    return await MockReactivationSource().pull_reactivation_records()


# ── copy ──────────────────────────────────────────────────────────────────
def _body(lang, seg, name=None):
    return reactivation_sms_body(lang, first_name=name, practice_name="Sol", segment=seg)


def test_sms_body_bilingual_and_segment_aware():
    es = _body("es", OVERDUE_RECALL, "Maria")
    en = _body("en", OVERDUE_RECALL, "John")
    assert es.startswith("Hola Maria") and "limpieza" in es and "STOP" in es
    assert en.startswith("Hi John") and "cleaning" in en
    assert "treatment" in _body("en", DROPPED_TREATMENT)  # segment changes reason
    assert _body("fr", LAPSED).startswith("Hi,")  # unknown lang → english


# ── result mapping ────────────────────────────────────────────────────────
async def test_send_reactivation_sms_maps_results(monkeypatch):
    p = Patient(id=uuid.uuid4(), practice_id=uuid.uuid4(), pms_external_id="x",
                first_name="A", phone="+15551230000")

    async def fake(*a, **k):
        return fake.ret
    monkeypatch.setattr(outreach_mod, "send_sms", fake)

    async def _send():
        return await send_reactivation_sms(p, "en", practice_name="Sol", segment=LAPSED)

    fake.ret = {"sent": True, "sid": "SM1"}
    r = await _send()
    assert (r.status, r.outcome, r.provider_ref) == ("sent", "delivered", "SM1")
    fake.ret = {"skipped": "opted_out"}
    assert (await _send()).outcome == "opt_out"
    fake.ret = {"error": "boom"}
    assert (await _send()).status == "failed"
    fake.ret = {"skipped": "sms_disabled"}
    assert (await _send()).status == "skipped"


# ── worker tick ───────────────────────────────────────────────────────────
async def _count_touches(db_session, practice):
    await set_tenant(db_session, practice.id)
    return (await db_session.execute(
        select(func.count()).select_from(ReactivationTouch)
        .where(ReactivationTouch.practice_id == practice.id)
    )).scalar_one()


async def test_worker_makes_touch_and_advances_cadence(db_session):
    practice, _ = await seed_practice(
        db_session, name="OR1", clerk_org_id="org_or1", clerk_user_id="u_or1"
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=_CFG_2SMS
    )
    await launch_campaign(db_session, practice.id, camp.id)

    s1 = await process_due_targets(db_session, practice.id, config=_CFG_2SMS)
    assert s1["processed"] >= 1
    assert await _count_touches(db_session, practice) == s1["processed"]
    await set_tenant(db_session, practice.id)
    tgts = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id)
    )).scalars().all()
    assert all(t.touches_count == 1 for t in tgts)

    # Second tick: step 2 (also SMS, delay 0) → cadence exhausted → no_answer.
    await process_due_targets(db_session, practice.id, config=_CFG_2SMS)
    await set_tenant(db_session, practice.id)
    tgts = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id)
    )).scalars().all()
    assert all(t.touches_count == 2 and t.status == "no_answer" and t.next_touch_at is None
               for t in tgts)


async def test_worker_terminates_on_opt_out(db_session):
    practice, _ = await seed_practice(
        db_session, name="OR2", clerk_org_id="org_or2", clerk_user_id="u_or2"
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=_CFG_2SMS
    )
    await launch_campaign(db_session, practice.id, camp.id)
    # A patient opts out AFTER enrollment.
    await set_tenant(db_session, practice.id)
    tgt = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id).limit(1)
    )).scalar_one()
    patient = (await db_session.execute(
        select(Patient).where(Patient.id == tgt.patient_id)
    )).scalar_one()
    patient.sms_opt_out = True
    await db_session.commit()

    s = await process_due_targets(db_session, practice.id, config=_CFG_2SMS)
    assert s["opted_out"] >= 1
    await set_tenant(db_session, practice.id)
    refreshed = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.id == tgt.id)
    )).scalar_one()
    assert refreshed.status == "opted_out" and refreshed.next_touch_at is None


async def test_worker_defers_voice_step(db_session):
    practice, _ = await seed_practice(
        db_session, name="OR3", clerk_org_id="org_or3", clerk_user_id="u_or3"
    )
    cfg = CampaignConfig(
        cadence=(CadenceStep("sms", 0), CadenceStep("voice", 0)),
        quiet_start_hour=0, quiet_end_hour=24,
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=cfg
    )
    await launch_campaign(db_session, practice.id, camp.id)

    await process_due_targets(db_session, practice.id, config=cfg)       # step0 sms
    s2 = await process_due_targets(db_session, practice.id, config=cfg)  # step1 voice → deferred
    assert s2["deferred"] >= 1 and s2["processed"] == 0
