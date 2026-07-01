"""System prompt + transcript mapping for the Groq receptionist relay.

The canonical, richly-tuned prompt lives in the voice repo (front_desk_v1/
system_prompt.md) for the Retell-managed path. When we run our OWN Groq relay we
need the prompt server-side; this builds a compact, clinic-aware version from the
practice row (name, hours, languages, transfer line) so the agent behaves as *this*
clinic. Deliberately short — every token is latency on a live call.
"""

from __future__ import annotations

from app.models.practice import Practice

# Base receptionist behavior. Kept tight; per-clinic facts are appended.
_BASE = (
    "You are the AI receptionist for {practice_name}, a US dental practice. "
    "Be warm, concise, and efficient. Max two sentences per turn, one question at "
    "a time — never compound questions. Speak at 130-140 wpm; if interrupted, stop "
    "immediately.\n"
    "LANGUAGE: detect the caller's language from their first words and conduct the "
    "ENTIRE call in that language — English or Spanish only. Follow the caller if "
    "they switch.\n"
    "SCOPE: help with booking, rescheduling, cancelling, hours, and location. Never "
    "diagnose, quote prices, or guarantee insurance coverage — offer a callback "
    "instead.\n"
    "EMERGENCY: if the caller mentions bleeding that won't stop, swelling, trouble "
    "breathing, a knocked-out tooth, or says it's an emergency, stop scheduling and "
    "say you're connecting them to the team right now (and to call 911 if it "
    "worsens).\n"
    "Confirm any phone number by reading it back digit by digit before using it."
)


def build_system_prompt(practice: Practice) -> str:
    """Compose the clinic-aware system prompt for the relay."""
    name = practice.name or "our office"
    parts = [_BASE.format(practice_name=name)]

    hours = practice.business_hours or {}
    if hours:
        # business_hours is a JSONB map; render a short line if present.
        parts.append(f"HOURS: {_render_hours(hours)}.")
    if practice.address:
        parts.append(f"LOCATION: {practice.address} (offer to text it).")
    langs = list(practice.languages_enabled or [])
    if langs:
        parts.append(f"Languages enabled: {', '.join(langs)}.")

    # Optional per-clinic knowledge base (FAQ-style facts the clinic supplied).
    kb = practice.knowledge_base or {}
    facts = kb.get("facts") if isinstance(kb, dict) else None
    if isinstance(facts, list) and facts:
        # Cap BOTH count and length: clinic-supplied text goes verbatim into the
        # system prompt, so bound it to limit latency + prompt-injection surface.
        joined = " ".join(str(f) for f in facts[:8])[:600]
        parts.append(f"CLINIC FACTS (use only these for specifics): {joined}")

    return "\n".join(parts)


def _render_hours(hours: dict) -> str:
    """Best-effort short render of a business_hours map; tolerant of shape."""
    try:
        days = [f"{d} {v}" for d, v in hours.items() if v]
        return "; ".join(days[:7]) or "call for hours"
    except Exception:  # noqa: BLE001 — never let prompt-building crash a call
        return "call for hours"


def transcript_to_messages(system_prompt: str, transcript: list[dict]) -> list[dict]:
    """Map Retell's transcript (role agent/user) to OpenAI-shaped chat messages."""
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for turn in transcript or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if not content:
            continue
        # Retell uses "agent" for the assistant; everything else is the caller.
        messages.append(
            {"role": "assistant" if role == "agent" else "user", "content": content}
        )
    return messages
