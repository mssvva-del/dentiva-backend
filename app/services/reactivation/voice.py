"""Outbound voice orchestration via Retell (Phase 1, block 7).

Places the reactivation VOICE touch: an outbound call from our Retell agent to a
dormant patient, in their language. Built + tested against a MOCKED Retell API
(httpx.MockTransport). The live call path is GATED on a real US number attached
to Retell (RETELL_FROM_NUMBER) — until that exists, ``get_voice_sender`` returns
None and the worker DEFERS voice touches (no touch lost), exactly as in block 6.

Two halves:
  * placement — ``make_voice_touch`` initiates the call and records it as 'sent'
    with the Retell call_id as provider_ref. The OUTCOME isn't known yet.
  * outcome — ``apply_voice_outcome`` is called later from the Retell webhook
    (call_analyzed) to stamp the touch result and, if the patient booked, flip
    the target to 'booked' and stop further touches.

WHY a placed call is NOT auto-retried: a retried create-call could double-dial a
patient. Like a PMS write, it's single-shot; the cadence's next step is the retry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import set_tenant
from app.models.patient import Patient
from app.models.reactivation import ReactivationTarget, ReactivationTouch
from app.services.reactivation.messages import _REASON  # reuse the reason copy
from app.services.reactivation.outreach import TouchResult
from app.utils.resilience import make_timeout

logger = logging.getLogger("dentiva.reactivation.voice")

RETELL_BASE = "https://api.retellai.com"


class RetellOutboundClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        from_number: str | None = None,
        base_url: str = RETELL_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> None:
        s = get_settings()
        self._api_key = api_key or s.retell_api_key
        self._from_number = from_number or s.retell_from_number
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout or make_timeout(s.http_connect_timeout, s.http_read_timeout)

    async def create_call(
        self,
        to_number: str,
        *,
        agent_id: str | None = None,
        dynamic_variables: dict | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Initiate one outbound call. Returns Retell's response (incl. call_id).
        Single-shot — never retried (a retry could double-dial)."""
        body: dict = {"from_number": self._from_number, "to_number": to_number}
        if agent_id:
            body["override_agent_id"] = agent_id
        if dynamic_variables:
            body["retell_llm_dynamic_variables"] = dynamic_variables
        if metadata:
            body["metadata"] = metadata
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.post(
                "/v2/create-phone-call",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if resp.status_code >= 400:
            # PHI-safe: status only, never the body (could echo the number).
            raise RuntimeError(f"Retell create-call failed {resp.status_code}")
        return resp.json()


def _agent_for(language: str) -> str | None:
    """Pick the OUTBOUND agent. Prefer the dedicated outbound agent (empty
    begin_message + delay so it WAITS for the person to say 'hello' — talking over
    them causes an instant hangup, per the B&H campaign data). Spanish-specific
    agent wins if configured; falls back to the inbound agent only if no outbound
    agent is set."""
    s = get_settings()
    if (language or "").lower().startswith("es") and s.retell_agent_id_es:
        return s.retell_agent_id_es
    return s.retell_outbound_agent_id or s.retell_agent_id or None


async def make_voice_touch(
    patient: Patient,
    language: str,
    target: ReactivationTarget,
    *,
    client: RetellOutboundClient,
    campaign=None,          # ReactivationCampaign | None — custom-campaign context
    practice_name: str = "",
    agent_name: str = "Alex",
    custom_greeting: str = "",
) -> TouchResult:
    """Place the outbound reactivation call. 'sent' on initiation; outcome stays
    None until the Retell webhook reports the result (apply_voice_outcome)."""
    lang = "es" if (language or "en").lower().startswith("es") else "en"
    reason = _REASON[lang].get(target.segment, _REASON[lang]["default"])
    # Custom campaigns: the clinic's own direction becomes the call's reason.
    # TCPA gate (reviewer): the SAME promo rule as SMS applies BEFORE dialing —
    # the agent is prompted to state the reason, so unattested promo context
    # reaching the call is a violation exactly like a promo text.
    campaign_context = ""
    if campaign is not None and getattr(campaign, "custom_context", None):
        allow_promo = (
            getattr(campaign, "category", "treatment") == "marketing"
            and getattr(campaign, "consent_attested_by", None) is not None
        )
        if not allow_promo:
            from app.services.reactivation.compliance import find_promo_violations
            violations = find_promo_violations(campaign.custom_context)
            if violations:
                logger.error(
                    "reactivation voice BLOCKED (TCPA promo gate): %s", violations
                )
                return TouchResult("skipped", "content_blocked")
        campaign_context = campaign.custom_context
        reason = campaign_context or reason
    try:
        res = await client.create_call(
            patient.phone,
            agent_id=_agent_for(lang),
            # The agent's OUTBOUND prompt section keys off these ({{direction}},
            # {{patient_first_name}}, {{practice_name}}, {{campaign_context}}).
            dynamic_variables={
                "direction": "outbound",
                "first_name": patient.first_name or "",
                "patient_first_name": patient.first_name or "",
                "practice_name": practice_name or "our dental office",
                "campaign_context": campaign_context,
                "language": lang,
                "reason": reason,
                # Persona vars — the begin_message/prompt reference {{agent_name}}
                # and {{custom_greeting}}; a missing key would be SPOKEN literally.
                "agent_name": agent_name or "Alex",
                "custom_greeting": custom_greeting or "",
            },
            # Correlate the eventual webhook back to THIS target/touch.
            metadata={"kind": "reactivation", "reactivation_target_id": str(target.id)},
        )
    except Exception:  # noqa: BLE001 — a failed dial must not crash the worker tick
        logger.exception("reactivation voice call failed for target %s", target.id)
        return TouchResult("failed", None)
    return TouchResult("sent", None, provider_ref=res.get("call_id"))


def get_voice_sender():  # noqa: ANN201
    """Return a voice sender callable for ``process_due_targets``, or None when no
    real number is attached yet (→ worker defers voice touches)."""
    s = get_settings()
    if not (s.retell_api_key and s.retell_from_number):
        return None
    client = RetellOutboundClient()

    async def _sender(patient: Patient, language: str, target: ReactivationTarget,
                      **context) -> TouchResult:
        return await make_voice_touch(patient, language, target, client=client,
                                      **context)

    return _sender


async def apply_voice_outcome(
    session: AsyncSession,
    practice_id: uuid.UUID,
    retell_call_id: str,
    *,
    booked: bool,
    booking_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> bool:
    """Called from the Retell webhook when an outbound reactivation call ends.

    Stamps the voice touch's outcome and, if the patient booked, flips the target
    to 'booked' (+ booking_id) and stops the cadence. A no-answer leaves the
    target alone — the cadence already advanced when the call was placed.
    Returns False if no reactivation touch matches (i.e. not our outbound call).
    """
    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)
    touch = (
        await session.execute(
            select(ReactivationTouch).where(
                ReactivationTouch.practice_id == practice_id,
                ReactivationTouch.provider_ref == retell_call_id,
                ReactivationTouch.channel == "voice",
            )
        )
    ).scalar_one_or_none()
    if touch is None:
        return False
    touch.outcome = "booked" if booked else "no_answer"
    if booked:
        target = (
            await session.execute(
                select(ReactivationTarget).where(ReactivationTarget.id == touch.target_id)
            )
        ).scalar_one()
        target.status = "booked"
        target.booking_id = booking_id
        target.next_touch_at = None  # success — stop contacting
    await session.commit()
    return True
