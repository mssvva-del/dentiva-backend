"""Outbound orchestration — the worker that makes reactivation touches (block 6).

Each tick: pull due targets (highest-value first) → make the current cadence
touch in the patient's language → record a ReactivationTouch → advance the target
to the next cadence step (or terminate). Block 6 implements the SMS channel; the
voice channel is dispatched the same way but its sender arrives in block 7
(gated on a real number) — until then voice steps are DEFERRED, not skipped, so
no touch is lost.

Outcome semantics for SMS: we can't know from a send alone whether the patient
books — that arrives later via the inbound two-way-SMS handler. So a delivered
text records outcome 'delivered'; booking attribution (target → booking_id,
status 'booked') is wired in the ROI block (9). Cadence exhaustion with no
positive signal terminates as 'no_answer'.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_tenant
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.reactivation import (
    ReactivationCampaign,
    ReactivationTarget,
    ReactivationTouch,
)
from app.services.reactivation.campaign import select_due_targets
from app.services.reactivation.compliance import (
    PromoContentBlocked,
    assert_treatment_only,
    frequency_block_reason,
)
from app.services.reactivation.messages import (
    custom_sms_body,
    reactivation_sms_body,
)
from app.services.reactivation.scheduling import CampaignConfig, next_allowed_time
from app.services.sms import send_sms

logger = logging.getLogger("dentiva.reactivation")


@dataclass
class TouchResult:
    status: str            # 'sent' | 'failed' | 'skipped'
    outcome: str | None    # 'delivered' | 'opt_out' | None
    provider_ref: str | None = None


async def send_reactivation_sms(
    patient: Patient,
    language: str,
    *,
    practice_name: str,
    segment: str,
    campaign: ReactivationCampaign | None = None,
    client: httpx.AsyncClient | None = None,
) -> TouchResult:
    """Send one reactivation SMS; map the fail-safe send_sms result → TouchResult.
    Respects the patient's opt-out (send_sms short-circuits on opted_out).

    Custom campaigns (segment='custom') use the clinic's own message. The TCPA
    promo-gate applies to EVERYTHING except marketing campaigns that carry a
    written-consent attestation — that's the only sanctioned promo path."""
    if campaign is not None and campaign.segment == "custom":
        body = custom_sms_body(
            language, first_name=patient.first_name,
            practice_name=practice_name, context=campaign.custom_context,
        )
    else:
        body = reactivation_sms_body(
            language, first_name=patient.first_name,
            practice_name=practice_name, segment=segment,
        )
    # TCPA content gate: treatment-only copy or nothing — one promo word
    # reclassifies the text as marketing ($500–1,500/message). Attested
    # marketing campaigns are the sole exemption (PEWC warranty recorded).
    allow_promo = (
        campaign is not None
        and campaign.category == "marketing"
        and campaign.consent_attested_by is not None
    )
    if not allow_promo:
        try:
            assert_treatment_only(body)
        except PromoContentBlocked:
            return TouchResult("skipped", "content_blocked")
    res = await send_sms(patient.phone, body, opted_out=patient.sms_opt_out, client=client)
    if res.get("sent"):
        return TouchResult("sent", "delivered", res.get("sid"))
    if res.get("skipped") == "opted_out":
        return TouchResult("skipped", "opt_out")
    if "error" in res:
        return TouchResult("failed", None)
    # disabled / not-configured / bad-number — attempted but not delivered.
    return TouchResult("skipped", None)


async def process_due_targets(
    session: AsyncSession,
    practice_id: uuid.UUID,
    *,
    now: datetime | None = None,
    config: CampaignConfig = CampaignConfig(),
    sms_client: httpx.AsyncClient | None = None,
    voice_sender=None,  # noqa: ANN001 — set in block 7
    limit: int = 100,
) -> dict:
    """One worker tick for a practice. Returns a small summary dict."""
    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)
    practice = (
        await session.execute(select(Practice).where(Practice.id == practice_id))
    ).scalar_one()
    due = await select_due_targets(session, practice_id, now=now, limit=limit)

    # Preload the campaigns behind this tick's targets (custom-campaign copy,
    # category/attestation for the promo-gate, channels).
    campaign_ids = {t.campaign_id for t in due}
    campaigns: dict[uuid.UUID, ReactivationCampaign] = {}
    if campaign_ids:
        rows = (await session.execute(
            select(ReactivationCampaign)
            .where(ReactivationCampaign.id.in_(campaign_ids))
        )).scalars().all()
        campaigns = {c.id: c for c in rows}

    summary = {"processed": 0, "sent": 0, "failed": 0, "deferred": 0, "opted_out": 0}
    for target in due:
        step = config.cadence[min(target.touches_count, len(config.cadence) - 1)]

        # Voice channel handled in block 7 — defer (leave the target untouched)
        # rather than skip, so the voice touch isn't lost.
        if step.channel == "voice" and voice_sender is None:
            summary["deferred"] += 1
            continue

        patient = (
            await session.execute(select(Patient).where(Patient.id == target.patient_id))
        ).scalar_one()
        lang = patient.preferred_language or "en"

        # TCPA: a patient who opted out AFTER enrollment terminates here.
        if patient.sms_opt_out:
            _record_touch(session, target, step.channel, "skipped", "opt_out", lang, None, now)
            target.status = "opted_out"
            target.next_touch_at = None
            summary["opted_out"] += 1
            summary["processed"] += 1
            continue

        # TCPA frequency caps (FCC 20-186): ≤1/day, ≤3/week per patient ACROSS
        # campaigns. Over the cap → DEFER (touch preserved, retried after the
        # window rolls), never silently skip.
        block = await frequency_block_reason(session, target.patient_id, now=now)
        if block is not None:
            target.next_touch_at = next_allowed_time(
                now + timedelta(days=1 if block == "daily_limit" else 2),
                practice.timezone, config.quiet_start_hour, config.quiet_end_hour,
            )
            summary["deferred"] += 1
            continue

        campaign = campaigns.get(target.campaign_id)
        if step.channel == "sms":
            result = await send_reactivation_sms(
                patient, lang, practice_name=practice.name,
                segment=target.segment, campaign=campaign, client=sms_client,
            )
        else:  # voice (block 7 sender provided)
            result = await voice_sender(
                patient, lang, target,
                campaign=campaign, practice_name=practice.name,
            )

        _record_touch(
            session, target, step.channel, result.status, result.outcome,
            lang, result.provider_ref, now,
        )
        # Flush NOW so the frequency-cap count sees this touch within the same
        # tick — a patient enrolled in two campaigns (two due targets) must not
        # receive two texts today. Autoflush would usually cover this, but the
        # TCPA invariant must not depend on session configuration.
        await session.flush()
        target.touches_count += 1
        summary["processed"] += 1
        summary["sent" if result.status == "sent" else "failed"] += (
            1 if result.status in ("sent", "failed") else 0
        )
        _advance(target, config, practice.timezone, now)

    await session.commit()
    if summary["processed"] or summary["deferred"]:
        logger.info("reactivation outreach tick practice=%s %s", practice_id, summary)
    return summary


def _record_touch(session, target, channel, status, outcome, language, provider_ref, now):  # noqa: ANN001
    session.add(ReactivationTouch(
        id=uuid.uuid4(), practice_id=target.practice_id, target_id=target.id,
        channel=channel, status=status, outcome=outcome, language=language,
        provider_ref=provider_ref, occurred_at=now,
    ))


def _advance(target: ReactivationTarget, config: CampaignConfig, tz_name, now) -> None:  # noqa: ANN001
    """Move the target to its next cadence step, or terminate if exhausted."""
    if target.touches_count >= len(config.cadence):
        # Cadence done with no booking observed → no_answer. A booking (inbound
        # two-way SMS / ROI block) flips this to 'booked' + sets booking_id.
        target.status = "no_answer"
        target.next_touch_at = None
        return
    nxt = config.cadence[target.touches_count]
    # delay_days are measured from ENROLLMENT (target.created_at) — the campaign
    # stretches over ~weeks, not from the previous touch.
    base = target.created_at + timedelta(days=nxt.delay_days)
    target.next_touch_at = next_allowed_time(
        base, tz_name, config.quiet_start_hour, config.quiet_end_hour
    )
    target.status = "pending"


# Local import alias to avoid a hard import cycle at module load.
async def set_tenant_for(session: AsyncSession, practice_id: uuid.UUID) -> None:
    from app.db import set_tenant

    await set_tenant(session, practice_id)
