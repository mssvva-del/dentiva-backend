"""Per-call dynamic variables for the Retell agent (KB → prompt bridge, V1).

Retell hits our inbound webhook BEFORE answering a call; we return these
variables and the agent's prompt references them as {{practice_name}},
{{kb_context}}, {{today}}, … — that's what turns the generic receptionist into
THIS clinic's receptionist (providers, insurances, policies) and lets it resolve
"next Tuesday" against the clinic's real local date.

All values MUST be strings (Retell requirement). Everything is bounded so a big
KB can't bloat the prompt.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.practice import Practice
from app.services.availability import office_status
from app.services.llm.prompt import _render_hours, _render_kb


def _clinic_now(tz_name: str | None) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.utcnow()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.utcnow()


def build_dynamic_variables(practice: Practice) -> dict[str, str]:
    """Everything the live agent should know about THIS clinic on THIS call."""
    now = _clinic_now(practice.timezone)
    kb = _render_kb(practice.knowledge_base or {})
    hours = _render_hours(practice.business_hours or {}) if practice.business_hours else ""
    langs = ", ".join(practice.languages_enabled or []) or "en"
    status, eta = office_status(practice, now=now)

    # Per-practice agent persona (onboarding step 5 / doctor panel). The canonical
    # opening lives in the Retell agent's begin_message, NOT here, and carries two
    # disclosures a clinic must not be able to remove: that the caller is talking
    # to an AI, and that the call is recorded. The clinic customizes only the
    # assistant's NAME and an OPTIONAL extra line, both substituted INTO that
    # sentence, so no custom text can strip either disclosure.
    #
    # This comment previously claimed the AI disclosure was already there. It was
    # not — the live greeting said only "this call may be recorded" until it was
    # corrected. A comment asserting a legal control that does not exist is worse
    # than no comment: the next person reads it and does not check.
    # Recording consent is all-party in some states (Massachusetts among them,
    # where the first clinic operates), which is why the wording is "is recorded"
    # rather than "may be". Keys must always be present (Retell substitutes only provided
    # vars — a missing key would leave a literal "{{agent_name}}" spoken aloud).
    agent = practice.agent_settings or {}
    agent_name = (str(agent.get("agent_name") or "").strip() or "Alex")[:60]
    custom_greeting = str(agent.get("greeting") or "").strip()[:300]

    return {
        "agent_name": agent_name,
        # Empty string when unset — the prompt treats it as "skip the extra line".
        "custom_greeting": custom_greeting,
        "practice_name": (practice.name or "our office")[:120],
        "practice_address": (practice.address or "")[:200],
        "practice_hours": hours[:300],
        "practice_languages": langs[:50],
        # Clinic-local calendar anchor — lets the agent resolve "tomorrow" /
        # "next Tuesday" correctly and never book into the past.
        "today": now.strftime("%A, %B %-d, %Y"),
        "current_time": now.strftime("%-I:%M %p"),
        "timezone": practice.timezone or "UTC",
        # Is anyone actually there right now? An urgent caller at 11pm must not be
        # promised a callback "in a few minutes" — and after hours a true medical
        # emergency has to be sent to the ER, not put in a queue.
        "office_status": status,
        "callback_eta": eta,
        # Destination for the NATIVE transfer_call tools. Empty string when the
        # clinic has given us no line to ring: the prompt then forbids transfers
        # and the agent takes an urgent callback instead — a transfer into a
        # number that doesn't exist is the dead-air bug all over again.
        "clinic_transfer_number": (
            practice.transfer_phone_number or practice.phone_number or ""
        ),
        # The structured clinic brain (providers/visit types/insurance/policies).
        "kb_context": kb if kb else "No additional clinic details on file.",
    }
