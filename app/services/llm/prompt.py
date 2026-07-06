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

    # Per-clinic knowledge base — the STRUCTURED object filled by onboarding /
    # analyze-website (schemas/knowledge_base.py: providers, appointment_types,
    # insurances, self_pay, policies, emergency). This is what makes the agent
    # "know" this clinic; render it into the prompt (bounded for latency).
    kb_block = _render_kb(practice.knowledge_base or {})
    if kb_block:
        parts.append(kb_block)

    return "\n".join(parts)


def _render_kb(kb: dict) -> str:
    """Render the structured KnowledgeBase JSONB into concise prompt lines. Tolerant
    of missing/partial sections; total length capped so a big KB doesn't blow up
    latency or the prompt-injection surface."""
    if not isinstance(kb, dict):
        return ""
    lines: list[str] = []

    providers = kb.get("providers")
    if isinstance(providers, list) and providers:
        rendered = []
        for p in providers[:12]:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            tag = p.get("type") or "provider"
            new = "" if p.get("accepts_new") is None else (
                ", accepting new" if p.get("accepts_new") else ", not taking new"
            )
            rendered.append(f"{p['name']} ({tag}{new})")
        if rendered:
            lines.append("PROVIDERS: " + "; ".join(rendered) + ".")

    appts = kb.get("appointment_types")
    if isinstance(appts, list) and appts:
        rendered = []
        for a in appts[:12]:
            if not isinstance(a, dict) or not a.get("name"):
                continue
            mins = f" ({a['minutes']}m)" if a.get("minutes") else ""
            rendered.append(f"{a['name']}{mins}")
        if rendered:
            lines.append("VISIT TYPES: " + "; ".join(rendered) + ".")

    ins = kb.get("insurances")
    ins_part = ""
    if isinstance(ins, list) and ins:
        ins_part = "In-network with " + ", ".join(str(i) for i in ins[:15]) + ". "
    if kb.get("self_pay") is True:
        ins_part += "Self-pay patients welcome. "
    if ins_part:
        lines.append(
            "INSURANCE: " + ins_part
            + "Never promise coverage — offer a callback to verify specifics."
        )

    pol = kb.get("policies")
    if isinstance(pol, dict):
        bits = [
            f"{label} {pol[key]}"
            for key, label in (
                ("cancellation", "Cancellation:"),
                ("late", "Late:"),
                ("new_patient", "New patients:"),
                ("parking", "Parking:"),
            )
            if pol.get(key)
        ]
        if bits:
            lines.append("POLICIES: " + " ".join(bits))

    emg = kb.get("emergency")
    if isinstance(emg, dict) and emg.get("on_call_number") and emg.get("action") == "transfer":
        lines.append("After-hours emergencies: transfer to the on-call line.")

    # Cap the whole block so a large KB can't bloat the system prompt.
    block = "\n".join(lines)
    return block[:1200]


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
