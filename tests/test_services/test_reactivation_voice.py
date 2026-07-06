"""Outbound voice orchestration (block 7) — Retell client + touch + outcome.

All against a MOCKED Retell API; no real calls. The live path is gated on a real
from-number (RETELL_FROM_NUMBER).
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

from app.adapters.nexhealth.mock import MockReactivationSource
from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.reactivation import ReactivationTarget, ReactivationTouch
from app.services.reactivation.campaign import build_campaign, launch_campaign
from app.services.reactivation.outreach import TouchResult, process_due_targets
from app.services.reactivation.scheduling import CadenceStep, CampaignConfig
from app.services.reactivation.segmentation import LAPSED
from app.services.reactivation.voice import (
    RetellOutboundClient,
    apply_voice_outcome,
    get_voice_sender,
    make_voice_touch,
)
from tests.conftest import seed_practice

_VOICE_CFG = CampaignConfig(
    cadence=(CadenceStep("voice", 0),), quiet_start_hour=0, quiet_end_hour=24
)


async def _records():
    return await MockReactivationSource().pull_reactivation_records()


def _client(handler) -> RetellOutboundClient:
    return RetellOutboundClient(
        api_key="K", from_number="+15550000000",
        transport=httpx.MockTransport(handler),
    )


# ── Retell client ─────────────────────────────────────────────────────────
async def test_create_call_builds_request_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/create-phone-call"
        assert request.headers.get("authorization") == "Bearer K"
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(201, json={"call_id": "call_abc"})

    res = await _client(handler).create_call(
        "+15551234567", agent_id="ag_1",
        dynamic_variables={"language": "es"}, metadata={"kind": "reactivation"},
    )
    assert res["call_id"] == "call_abc"
    assert seen["from_number"] == "+15550000000" and seen["to_number"] == "+15551234567"
    assert seen["override_agent_id"] == "ag_1"
    assert seen["retell_llm_dynamic_variables"]["language"] == "es"
    assert seen["metadata"]["kind"] == "reactivation"


async def test_create_call_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad"})

    try:
        await _client(handler).create_call("+15551234567")
        raise AssertionError("expected error")
    except RuntimeError:
        pass


# ── make_voice_touch ──────────────────────────────────────────────────────
async def test_make_voice_touch_sent_and_failed():
    p = Patient(id=uuid.uuid4(), practice_id=uuid.uuid4(), pms_external_id="x",
                first_name="Maria", phone="+15551112222", preferred_language="es")
    tgt = ReactivationTarget(id=uuid.uuid4(), practice_id=p.practice_id,
                             campaign_id=uuid.uuid4(), patient_id=p.id, segment=LAPSED)

    ok = _client(lambda r: httpx.Response(201, json={"call_id": "C9"}))
    res = await make_voice_touch(p, "es", tgt, client=ok)
    assert res.status == "sent" and res.provider_ref == "C9" and res.outcome is None

    bad = _client(lambda r: httpx.Response(500))
    assert (await make_voice_touch(p, "es", tgt, client=bad)).status == "failed"


# ── gating ────────────────────────────────────────────────────────────────
def test_get_voice_sender_none_without_number(settings):
    # No RETELL_FROM_NUMBER in test env → worker defers voice.
    assert get_voice_sender() is None


# ── worker makes a voice touch ────────────────────────────────────────────
async def test_worker_places_voice_touch(db_session):
    practice, _ = await seed_practice(
        db_session, name="V1", clerk_org_id="org_v1", clerk_user_id="u_v1"
    )
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=_VOICE_CFG
    )
    await launch_campaign(db_session, practice.id, camp.id)

    calls = {"n": 0}

    async def fake_voice(patient, language, target):
        calls["n"] += 1
        return TouchResult("sent", None, provider_ref=f"RC{calls['n']}")

    s = await process_due_targets(
        db_session, practice.id, config=_VOICE_CFG, voice_sender=fake_voice
    )
    assert s["processed"] >= 1 and s["deferred"] == 0
    await set_tenant(db_session, practice.id)
    touches = (await db_session.execute(
        select(ReactivationTouch).where(ReactivationTouch.practice_id == practice.id)
    )).scalars().all()
    assert touches and all(t.channel == "voice" and t.provider_ref for t in touches)


# ── apply_voice_outcome ───────────────────────────────────────────────────
async def _seed_voice_touch(db_session, practice, call_id):
    """Build+launch a voice campaign and place touches; return (target, touch)."""
    camp = await build_campaign(
        db_session, practice.id, LAPSED, await _records(), campaign_config=_VOICE_CFG
    )
    await launch_campaign(db_session, practice.id, camp.id)

    async def fake_voice(patient, language, target):
        return TouchResult("sent", None, provider_ref=call_id)

    await process_due_targets(
        db_session, practice.id, config=_VOICE_CFG, voice_sender=fake_voice
    )
    await set_tenant(db_session, practice.id)
    touch = (await db_session.execute(
        select(ReactivationTouch).where(ReactivationTouch.provider_ref == call_id)
    )).scalars().first()
    return touch


async def test_apply_outcome_booked_flips_target(db_session):
    practice, _ = await seed_practice(
        db_session, name="V2", clerk_org_id="org_v2", clerk_user_id="u_v2"
    )
    touch = await _seed_voice_touch(db_session, practice, "CALL_BOOK")
    # seed a booking to attribute
    await set_tenant(db_session, practice.id)
    target = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.id == touch.target_id)
    )).scalar_one()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=target.patient_id,
        appointment_at=touch.occurred_at, duration_minutes=60,
        status="confirmed", source="reactivation",
    )
    db_session.add(booking)
    await db_session.commit()

    ok = await apply_voice_outcome(
        db_session, practice.id, "CALL_BOOK", booked=True, booking_id=booking.id
    )
    assert ok is True
    await set_tenant(db_session, practice.id)
    target = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.id == touch.target_id)
    )).scalar_one()
    refreshed_touch = (await db_session.execute(
        select(ReactivationTouch).where(ReactivationTouch.id == touch.id)
    )).scalar_one()
    assert target.status == "booked" and target.booking_id == booking.id
    assert target.next_touch_at is None and refreshed_touch.outcome == "booked"


async def test_apply_outcome_no_answer_and_unknown(db_session):
    practice, _ = await seed_practice(
        db_session, name="V3", clerk_org_id="org_v3", clerk_user_id="u_v3"
    )
    touch = await _seed_voice_touch(db_session, practice, "CALL_NA")
    ok = await apply_voice_outcome(db_session, practice.id, "CALL_NA", booked=False)
    assert ok is True
    await set_tenant(db_session, practice.id)
    refreshed = (await db_session.execute(
        select(ReactivationTouch).where(ReactivationTouch.id == touch.id)
    )).scalar_one()
    assert refreshed.outcome == "no_answer"
    # unknown call id → not our touch
    assert await apply_voice_outcome(db_session, practice.id, "nope", booked=True) is False
