"""REACT-GUARD — TCPA guards: promo gate, frequency caps, quiet window, opt-out."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.reactivation.compliance import (
    PromoContentBlocked,
    assert_treatment_only,
    find_promo_violations,
    frequency_block_reason,
)
from app.services.reactivation.messages import reactivation_sms_body
from app.services.reactivation.scheduling import CampaignConfig
from app.services.reactivation.segmentation import DROPPED_TREATMENT, OVERDUE_RECALL
from app.webhooks.twilio_sms import _classify


# ── content gate ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Get 20% off whitening this month!",
    "We have a special offer for returning patients",
    "Free consultation if you book today",
    "Use coupon SMILE10 at checkout",
    "Limited time deal on cleanings",
    "Save $50 on your next visit",
    "Aproveche nuestro descuento del 15%",
    "Blanqueamiento gratis con su limpieza",
])
def test_promo_content_is_blocked(text):
    assert find_promo_violations(text)
    with pytest.raises(PromoContentBlocked):
        assert_treatment_only(text)


@pytest.mark.parametrize("text", [
    "Hi Anna, it's Bright Dental — you're due for a cleaning. Reply STOP to opt out.",
    "Feel free to call us back any time to reschedule.",
    "We'd love to help you finish your treatment plan.",
    "Hola, le escribimos de Clinica Sol: le toca su limpieza dental.",
])
def test_treatment_content_passes(text):
    assert find_promo_violations(text) == []
    assert_treatment_only(text)  # no raise


def test_all_builtin_templates_are_treatment_only():
    """Every language × segment template we ship must pass the gate."""
    for lang in ("en", "es"):
        for segment in (OVERDUE_RECALL, DROPPED_TREATMENT, "anything_else"):
            body = reactivation_sms_body(
                lang, first_name="Anna", practice_name="Bright Dental",
                segment=segment,
            )
            assert find_promo_violations(body) == [], (lang, segment, body)


# ── legal-safe quiet window default ──────────────────────────────────────────
def test_default_quiet_window_is_9_to_20():
    cfg = CampaignConfig()
    assert cfg.quiet_start_hour == 9
    assert cfg.quiet_end_hour == 20


# ── free-form opt-out classification ─────────────────────────────────────────
@pytest.mark.parametrize("body", [
    "STOP",                              # classic keyword still works
    "please stop texting me",
    "Don't message me again",
    "do not contact me",
    "no more texts please",
    "I want to opt out",
    "opt-out",
    "remove me from your list",
    "take me off this list",
    "leave me alone",
    "wrong number",
    "unsubscribe me please",
    "no me escriban más",
    "no más mensajes",
    "basta",
])
def test_freeform_revocations_classify_as_stop(body):
    assert _classify(body) == "stop"


@pytest.mark.parametrize("body,expected", [
    ("can we stop by tomorrow at 3?", "unknown"),   # 'stop by' ≠ revocation
    ("yes", "confirm"),
    ("no", "cancel"),                                # keyword path unchanged
    ("C please", "confirm"),
    ("what time works?", "unknown"),
    ("no problem, see you then", "cancel"),          # first-word rule preserved
    ("start", "start"),
])
def test_normal_replies_keep_their_intent(body, expected):
    assert _classify(body) == expected


# ── frequency caps ───────────────────────────────────────────────────────────
async def _seed_target_with_touches(db_session, *, touch_times, statuses=None):
    from app.models.patient import Patient
    from app.models.reactivation import (
        ReactivationCampaign,
        ReactivationTarget,
        ReactivationTouch,
    )
    from tests.conftest import seed_practice

    practice, _ = await seed_practice(
        db_session, name=f"FreqCo-{uuid.uuid4().hex[:6]}",
        clerk_org_id=f"o_{uuid.uuid4().hex[:8]}", clerk_user_id=f"u_{uuid.uuid4().hex[:8]}",
    )
    patient = Patient(id=uuid.uuid4(), practice_id=practice.id,
                      pms_external_id=f"ext-{uuid.uuid4().hex[:8]}",
                      first_name="Freq", last_name="Test", phone="+15551230000")
    campaign = ReactivationCampaign(id=uuid.uuid4(), practice_id=practice.id,
                                    name="c", segment="overdue_recall",
                                    status="active")
    db_session.add_all([patient, campaign])
    await db_session.flush()
    target = ReactivationTarget(id=uuid.uuid4(), practice_id=practice.id,
                                campaign_id=campaign.id, patient_id=patient.id,
                                segment="overdue_recall", status="active")
    db_session.add(target)
    await db_session.flush()
    statuses = statuses or ["sent"] * len(touch_times)
    for ts, st in zip(touch_times, statuses, strict=True):
        t = ReactivationTouch(id=uuid.uuid4(), practice_id=practice.id,
                              target_id=target.id, channel="sms", status=st)
        db_session.add(t)
        await db_session.flush()
        # created_at is server_default now() — override explicitly for the test.
        t.created_at = ts
    await db_session.commit()
    return patient


async def test_daily_cap_blocks_second_touch(db_session):
    now = datetime.now(tz=UTC)
    patient = await _seed_target_with_touches(
        db_session, touch_times=[now - timedelta(hours=2)])
    assert await frequency_block_reason(db_session, patient.id, now=now) == "daily_limit"


async def test_weekly_cap_blocks_fourth_touch(db_session):
    now = datetime.now(tz=UTC)
    patient = await _seed_target_with_touches(
        db_session,
        touch_times=[now - timedelta(days=2), now - timedelta(days=4),
                     now - timedelta(days=6)],
    )
    assert await frequency_block_reason(db_session, patient.id, now=now) == "weekly_limit"


async def test_caps_allow_when_window_rolled(db_session):
    now = datetime.now(tz=UTC)
    patient = await _seed_target_with_touches(
        db_session,
        touch_times=[now - timedelta(days=8), now - timedelta(days=9)],
    )
    assert await frequency_block_reason(db_session, patient.id, now=now) is None


async def test_failed_and_skipped_touches_do_not_consume_allowance(db_session):
    now = datetime.now(tz=UTC)
    patient = await _seed_target_with_touches(
        db_session,
        touch_times=[now - timedelta(hours=1), now - timedelta(hours=3)],
        statuses=["failed", "skipped"],
    )
    assert await frequency_block_reason(db_session, patient.id, now=now) is None


async def test_same_patient_in_two_campaigns_gets_one_sms_per_tick(db_session, monkeypatch):
    """Reviewer 🔴: a patient enrolled in TWO campaigns has two due targets in the
    same worker tick — the daily cap must apply WITHIN the tick (the first send is
    flushed and visible to the second target's frequency check), so exactly one
    SMS goes out and the other target defers."""
    from sqlalchemy import func, select

    from app.adapters.nexhealth.mock import MockReactivationSource
    from app.db import set_tenant
    from app.models.reactivation import ReactivationTarget, ReactivationTouch
    from app.services.reactivation import outreach as outreach_mod
    from app.services.reactivation.campaign import build_campaign, launch_campaign
    from app.services.reactivation.outreach import process_due_targets
    from app.services.reactivation.scheduling import CadenceStep
    from app.services.reactivation.segmentation import LAPSED
    from tests.conftest import seed_practice

    async def _fake_send(phone, body, *, opted_out=False, client=None):  # noqa: ANN001
        return {"sent": True, "sid": f"SM{uuid.uuid4().hex[:8]}"}
    monkeypatch.setattr(outreach_mod, "send_sms", _fake_send)

    practice, _ = await seed_practice(
        db_session, name="TwoCampCo", clerk_org_id="o_twocamp", clerk_user_id="u_twocamp")
    cfg = CampaignConfig(cadence=(CadenceStep("sms", 0),),
                         quiet_start_hour=0, quiet_end_hour=24)
    records = await MockReactivationSource().pull_reactivation_records()
    for _ in range(2):  # same patients, two campaigns → two targets per patient
        camp = await build_campaign(db_session, practice.id, LAPSED, records,
                                    campaign_config=cfg)
        await launch_campaign(db_session, practice.id, camp.id)

    summary = await process_due_targets(db_session, practice.id, config=cfg)
    assert summary["deferred"] >= 1  # second target per patient hit the daily cap

    await set_tenant(db_session, practice.id)
    rows = (await db_session.execute(
        select(ReactivationTarget.patient_id, func.count())
        .select_from(ReactivationTouch)
        .join(ReactivationTarget, ReactivationTouch.target_id == ReactivationTarget.id)
        .where(ReactivationTouch.status == "sent")
        .group_by(ReactivationTarget.patient_id)
    )).all()
    assert rows, "expected at least one sent touch"
    assert all(count == 1 for _pid, count in rows), rows  # never 2 texts same day


# ---------------------------------------------------------------------------
# A ceiling for the whole practice, not just per patient.
#
# The caps above stop us pestering one person. Nothing stopped a runaway campaign
# or a mis-parsed import from dialling an entire list — and the HTTP rate limiter
# cannot help: it lives in process memory, so it multiplies by the instance count
# and resets on deploy, and the outbound worker never goes through HTTP anyway.
# Every attempt is real money leaving the account and, in the US, a TCPA exposure.
# ---------------------------------------------------------------------------


async def _practice_with_touches(db_session, count: int, *, status: str = "sent"):
    import uuid as _uuid
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.models.patient import Patient
    from app.models.reactivation import (
        ReactivationCampaign,
        ReactivationTarget,
        ReactivationTouch,
    )
    from tests.conftest import seed_practice

    practice, _ = await seed_practice(
        db_session, name=f"CeilCo-{_uuid.uuid4().hex[:6]}",
        clerk_org_id=f"o_{_uuid.uuid4().hex[:8]}", clerk_user_id=f"u_{_uuid.uuid4().hex[:8]}",
    )
    patient = Patient(id=_uuid.uuid4(), practice_id=practice.id,
                      pms_external_id=f"ext-{_uuid.uuid4().hex[:8]}",
                      first_name="Ceil", last_name="Test", phone="+15551239999")
    campaign = ReactivationCampaign(id=_uuid.uuid4(), practice_id=practice.id,
                                    name="c", segment="overdue_recall", status="active")
    db_session.add_all([patient, campaign])
    await db_session.flush()
    target = ReactivationTarget(id=_uuid.uuid4(), practice_id=practice.id,
                                campaign_id=campaign.id, patient_id=patient.id,
                                segment="overdue_recall", status="active")
    db_session.add(target)
    await db_session.flush()
    for _ in range(count):
        t = ReactivationTouch(id=_uuid.uuid4(), practice_id=practice.id,
                              target_id=target.id, channel="voice", status=status)
        db_session.add(t)
        await db_session.flush()
        t.created_at = _dt.now(tz=_UTC) - timedelta(hours=1)
    await db_session.commit()
    return practice


async def test_a_practice_under_the_ceiling_keeps_dialling(db_session):
    from app.services.reactivation.compliance import (
        PRACTICE_MAX_TOUCHES_PER_DAY,
        practice_daily_ceiling_reached,
    )

    practice = await _practice_with_touches(db_session, PRACTICE_MAX_TOUCHES_PER_DAY - 1)
    assert await practice_daily_ceiling_reached(db_session, practice.id) is False


async def test_the_ceiling_stops_a_runaway_campaign(db_session):
    from app.services.reactivation.compliance import (
        PRACTICE_MAX_TOUCHES_PER_DAY,
        practice_daily_ceiling_reached,
    )

    practice = await _practice_with_touches(db_session, PRACTICE_MAX_TOUCHES_PER_DAY)
    assert await practice_daily_ceiling_reached(db_session, practice.id) is True


async def test_attempts_that_never_left_do_not_count_against_the_ceiling(db_session):
    """A skipped or failed touch cost nothing and reached nobody. Counting it
    would throttle a practice for our own errors."""
    from app.services.reactivation.compliance import (
        PRACTICE_MAX_TOUCHES_PER_DAY,
        practice_daily_ceiling_reached,
    )

    practice = await _practice_with_touches(
        db_session, PRACTICE_MAX_TOUCHES_PER_DAY, status="failed"
    )
    assert await practice_daily_ceiling_reached(db_session, practice.id) is False


async def test_one_practice_hitting_the_ceiling_does_not_stop_another(db_session):
    from app.services.reactivation.compliance import (
        PRACTICE_MAX_TOUCHES_PER_DAY,
        practice_daily_ceiling_reached,
    )

    loud = await _practice_with_touches(db_session, PRACTICE_MAX_TOUCHES_PER_DAY)
    quiet = await _practice_with_touches(db_session, 0)
    assert await practice_daily_ceiling_reached(db_session, loud.id) is True
    assert await practice_daily_ceiling_reached(db_session, quiet.id) is False
