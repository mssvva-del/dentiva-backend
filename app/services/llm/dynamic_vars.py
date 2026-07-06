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

    return {
        "practice_name": (practice.name or "our office")[:120],
        "practice_address": (practice.address or "")[:200],
        "practice_hours": hours[:300],
        "practice_languages": langs[:50],
        # Clinic-local calendar anchor — lets the agent resolve "tomorrow" /
        # "next Tuesday" correctly and never book into the past.
        "today": now.strftime("%A, %B %-d, %Y"),
        "current_time": now.strftime("%-I:%M %p"),
        "timezone": practice.timezone or "UTC",
        # The structured clinic brain (providers/visit types/insurance/policies).
        "kb_context": kb if kb else "No additional clinic details on file.",
    }
