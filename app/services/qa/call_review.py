"""Self-learning loop — auto-review FAILED calls, surface prompt fixes.

The B&H playbook's core insight (VOICE_KNOWLEDGE_TRANSFER §6): quality comes from
the CYCLE, not the script. Every batch, read the FAILURE transcripts (not the
wins), find where the caller dropped, and any pattern that repeats 3+ times
becomes a prompt fix. This service automates the read + pattern-find:

  1. pull recent calls with a failure outcome (+ transcript),
  2. LLM-analyze each → where it broke, why, ONE concrete prompt fix, a category,
  3. aggregate categories → patterns that repeat are the real signal,
  4. return findings + a ranked "suggested prompt changes" list.

It never auto-edits the prompt — a human reviews and applies (same rule as the
KB: no auto-write without a person). Analysis focuses on AGENT behaviour; the LLM
is told never to echo patient PII.

⚠️ Transcripts are PHI. The reviewing LLM must be BAA-covered before real
patients (Anthropic/Bedrock BAA); fine for the pre-launch pilot with test calls.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.call import Call
from app.services.call_outcome import FAILURE_OUTCOMES
from app.services.onboarding_ai import _anthropic_json

logger = logging.getLogger("dentiva.qa")


async def _llm_json(prompt: str) -> dict:
    """Anthropic-ONLY JSON call for QA.

    Transcripts are PHI, so — unlike onboarding, which analyses public website
    text — this path must NOT fall back to Groq (no BAA). If Anthropic is absent
    or errors, we raise and skip the call rather than leak PHI to a BAA-less
    vendor. (Anthropic needs a signed BAA before real patients regardless.)
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("qa_review_requires_anthropic")
    text = await _anthropic_json(prompt, settings.anthropic_api_key)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("qa_review_unparseable")
    return json.loads(match.group(0))

# FAILURE_OUTCOMES is the single source of truth in app.services.call_outcome —
# the same taxonomy the webhook writes, so QA and classification never drift.

_REVIEW_PROMPT = """You are a call-QA analyst improving an AI dental receptionist.
Given ONE call transcript and its outcome, judge the AGENT's performance only.

Return a SINGLE JSON object, no prose:
{
  "lost_caller": bool,               // did the agent lose/frustrate the caller?
  "break_point": str,                // short: WHERE it went wrong (a turn/topic), or ""
  "why": str,                        // one sentence, about the AGENT's behaviour
  "prompt_fix": str,                 // ONE concrete, actionable change to the agent prompt
  "category": str                    // kebab-case slug, e.g. "talked-over-caller",
                                     // "asked-twice", "misheard-digits", "no-clear-offer",
                                     // "gave-up-early", "wrong-emergency-handling"
}
Rules: never include patient names, phone numbers, or any PII in your answer —
refer to "the caller". If the agent did fine, lost_caller=false and prompt_fix="".

OUTCOME: {outcome}
TRANSCRIPT:
{transcript}
"""


def _flatten_transcript(t: dict | list | None) -> str:
    if isinstance(t, list):
        lines = [f"{x.get('role', '?')}: {x.get('content', '')}" for x in t
                 if isinstance(x, dict) and x.get("content")]
        return "\n".join(lines)[:8000]
    if isinstance(t, str):
        return t[:8000]
    return ""


async def analyze_call(transcript: dict | list | None, outcome: str | None) -> dict:
    """LLM review of one call → structured finding (bounded, PII-free by prompt)."""
    text = _flatten_transcript(transcript)
    if not text:
        return {"lost_caller": False, "break_point": "", "why": "",
                "prompt_fix": "", "category": "no-transcript"}
    prompt = _REVIEW_PROMPT.replace("{outcome}", str(outcome or "unknown")).replace(
        "{transcript}", text)
    raw = await _llm_json(prompt)
    return {
        "lost_caller": bool(raw.get("lost_caller")),
        "break_point": str(raw.get("break_point") or "")[:200],
        "why": str(raw.get("why") or "")[:300],
        "prompt_fix": str(raw.get("prompt_fix") or "")[:400],
        "category": re.sub(r"[^a-z0-9-]", "", str(raw.get("category") or "other").lower())[:40]
        or "other",
    }


# ---------------------------------------------------------------------------
# Broken-promise scan — deterministic, runs on EVERY call, not just failures.
#
# transfer_to_human once answered "transfer_initiated" while nothing bridged the
# call, so the agent said "let me connect you" and the caller heard silence. That
# call ends as a normal callback, so the failure-outcome review above would never
# look at it. Any promise we cannot keep is checked here by regex — no LLM, no
# outcome dependency, no PHI leaving the box.
# ---------------------------------------------------------------------------

_BROKEN_PROMISES = (
    (r"connect(ing)?\s+you", "promised to connect the caller — no live bridge exists"),
    (r"transferr?ing\s+you", "promised a transfer — no live bridge exists"),
    (r"put\s+you\s+through", "promised to put the caller through — no live bridge exists"),
    (r"stay\s+on\s+the\s+line\s+while", "asked the caller to hold for a person"),
    (r"one\s+moment\s+while\s+i\s+(get|connect|transfer)", "implied someone is coming on"),
)
_BROKEN_PROMISE_RES = tuple((re.compile(p, re.I), why) for p, why in _BROKEN_PROMISES)


def find_broken_promises(transcript: dict | list | None) -> list[str]:
    """Reasons this transcript contains a promise the product cannot keep.

    Agent turns only — a CALLER saying "can you connect me to a person" is a
    request, not a false promise.
    """
    turns: list[str] = []
    if isinstance(transcript, list):
        turns = [str(x.get("content") or "") for x in transcript
                 if isinstance(x, dict) and x.get("role") == "agent"]
    elif isinstance(transcript, str):
        # Flat transcripts are "Agent: ...\nUser: ..." — keep the agent lines.
        turns = [ln.split(":", 1)[1] for ln in transcript.splitlines()
                 if ln.lower().startswith("agent:") and ":" in ln]
    hits: list[str] = []
    for text in turns:
        for rx, why in _BROKEN_PROMISE_RES:
            if rx.search(text) and why not in hits:
                hits.append(why)
    return hits


async def review_recent_failures(
    session: AsyncSession, *, limit: int = 15
) -> dict:
    """Pull recent FAILED calls, review each, and roll up the patterns.

    Cross-clinic (coordinator improving the shared agent). Runs on the caller's
    admin session — in prod that's the superuser role, which sees every tenant;
    the endpoint gates this to super_admin.
    """
    # Scan EVERY recent call for promises we cannot keep — independent of outcome,
    # because a false "connecting you" usually ends as a perfectly normal call.
    recent = (await session.execute(
        select(Call.id, Call.transcript_jsonb, Call.outcome)
        .where(Call.transcript_jsonb.isnot(None))
        .order_by(Call.started_at.desc())
        .limit(max(limit, 50))
    )).all()
    broken_promises = [
        {"call_id": str(cid), "outcome": outcome, "reasons": reasons}
        for cid, tr, outcome in recent
        if (reasons := find_broken_promises(tr))
    ]

    rows = (await session.execute(
        select(Call)
        .where(Call.outcome.in_(tuple(FAILURE_OUTCOMES)))
        .where(Call.transcript_jsonb.isnot(None))
        .order_by(Call.started_at.desc())
        .limit(limit)
    )).scalars().all()

    findings: list[dict] = []
    for call in rows:
        try:
            f = await analyze_call(call.transcript_jsonb, call.outcome)
        except Exception as exc:  # noqa: BLE001 — one bad review can't kill the batch
            logger.warning("qa: review failed for call %s: %s", call.id, exc)
            continue
        f["call_id"] = str(call.id)
        f["outcome"] = call.outcome
        findings.append(f)

    # Pattern rollup: a category that repeats is the real signal (B&H 3+ rule).
    by_category: dict[str, dict] = {}
    for f in findings:
        if not f["lost_caller"]:
            continue
        c = f["category"]
        entry = by_category.setdefault(c, {"category": c, "count": 0, "fixes": []})
        entry["count"] += 1
        if f["prompt_fix"] and f["prompt_fix"] not in entry["fixes"]:
            entry["fixes"].append(f["prompt_fix"])
    patterns = sorted(by_category.values(), key=lambda e: e["count"], reverse=True)
    # "Actionable now" = a pattern seen 3+ times (worth a prompt change).
    for p in patterns:
        p["actionable"] = p["count"] >= 3
        p["fixes"] = p["fixes"][:3]

    return {
        "reviewed": len(findings),
        "lost_callers": sum(1 for f in findings if f["lost_caller"]),
        # Non-empty here means the agent is promising something we don't deliver —
        # treat as a release blocker, not a nice-to-have.
        "broken_promises": broken_promises,
        "patterns": patterns,
        "findings": findings,
    }
