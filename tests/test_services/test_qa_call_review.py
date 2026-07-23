"""QA-LOOP-1 — self-learning loop: review failed calls → prompt-fix patterns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.call import Call
from app.services.qa import call_review as qa
from tests.conftest import seed_practice


def _mk_call(practice_id, *, outcome, transcript, minutes_ago=0):
    return Call(
        id=uuid.uuid4(),
        practice_id=practice_id,
        retell_call_id=f"retell-{uuid.uuid4().hex[:8]}",
        direction="inbound",
        from_number="+15551112222",
        to_number="+15559876543",
        started_at=datetime.now(tz=UTC) - timedelta(minutes=minutes_ago),
        status="completed",
        outcome=outcome,
        transcript_jsonb=transcript,
    )


# ── unit: flatten + bounds ────────────────────────────────────────────────────
def test_flatten_transcript_handles_shapes():
    assert qa._flatten_transcript(None) == ""
    assert "agent: hi" in qa._flatten_transcript(
        [{"role": "agent", "content": "hi"}, {"role": "user", "content": "bye"}]
    )
    assert qa._flatten_transcript("raw text") == "raw text"
    # oversized list is bounded
    big = [{"role": "user", "content": "x" * 100}] * 500
    assert len(qa._flatten_transcript(big)) <= 8000


async def test_analyze_call_empty_transcript_short_circuits(monkeypatch):
    called = False

    async def _boom(_prompt):  # must NOT be called for empty transcript
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(qa, "_llm_json", _boom)
    out = await qa.analyze_call(None, "no_answer")
    assert out["category"] == "no-transcript"
    assert out["lost_caller"] is False
    assert called is False


async def test_analyze_call_bounds_and_sanitizes(monkeypatch):
    async def _fake(_prompt):
        return {
            "lost_caller": 1,  # truthy → bool
            "break_point": "x" * 999,
            "why": "agent talked over caller",
            "prompt_fix": "y" * 999,
            "category": "Talked Over Caller!!",  # → slug
        }

    monkeypatch.setattr(qa, "_llm_json", _fake)
    out = await qa.analyze_call([{"role": "agent", "content": "hi"}], "no_booking")
    assert out["lost_caller"] is True
    assert len(out["break_point"]) <= 200
    assert len(out["prompt_fix"]) <= 400
    assert out["category"] == "talkedovercaller"  # non-slug chars stripped


# ── rollup: pattern that repeats 3+ is "actionable" ──────────────────────────
async def test_review_rolls_up_patterns(monkeypatch, db_session):
    practice, _ = await seed_practice(
        db_session, name="QA Clinic", clerk_org_id="org_qa", clerk_user_id="u_qa"
    )
    # Unique transcript tokens — NOT words that appear in _REVIEW_PROMPT's example
    # slugs (e.g. "digits"/"emergency"), else the _fake matcher collides.
    # 3 calls broken the same way + 1 different + 1 clean
    for i in range(3):
        db_session.add(_mk_call(
            practice.id, outcome="no_booking",
            transcript=[{"role": "agent", "content": f"tokenAlpha {i}"}], minutes_ago=i,
        ))
    db_session.add(_mk_call(
        practice.id, outcome="abandoned",
        transcript=[{"role": "agent", "content": "tokenBravo"}], minutes_ago=10,
    ))
    db_session.add(_mk_call(
        practice.id, outcome="no_booking",
        transcript=[{"role": "agent", "content": "tokenClean"}], minutes_ago=20,
    ))
    await db_session.commit()

    async def _fake(prompt):
        if "tokenBravo" in prompt:
            return {"lost_caller": True, "break_point": "emergency", "why": "w",
                    "prompt_fix": "handle emergencies first", "category": "wrong-emergency"}
        if "tokenClean" in prompt:
            return {"lost_caller": False, "category": "ok", "prompt_fix": ""}
        return {"lost_caller": True, "break_point": "digits", "why": "misheard",
                "prompt_fix": "confirm digits back", "category": "misheard-digits"}

    monkeypatch.setattr(qa, "_llm_json", _fake)
    result = await qa.review_recent_failures(db_session, limit=15)

    assert result["reviewed"] == 5
    assert result["lost_callers"] == 4
    cats = {p["category"]: p for p in result["patterns"]}
    # misheard-digits seen 3× → actionable; wrong-emergency 1× → not
    assert cats["misheard-digits"]["count"] == 3
    assert cats["misheard-digits"]["actionable"] is True
    assert cats["wrong-emergency"]["actionable"] is False
    # ranked by count desc
    assert result["patterns"][0]["category"] == "misheard-digits"
    # deduped fixes, capped 3
    assert cats["misheard-digits"]["fixes"] == ["confirm digits back"]


async def test_review_survives_one_bad_analysis(monkeypatch, db_session):
    practice, _ = await seed_practice(
        db_session, name="QA2", clerk_org_id="org_qa2", clerk_user_id="u_qa2"
    )
    db_session.add(_mk_call(practice.id, outcome="no_booking",
                            transcript=[{"role": "agent", "content": "boom"}]))
    db_session.add(_mk_call(practice.id, outcome="no_booking",
                            transcript=[{"role": "agent", "content": "fine"}]))
    await db_session.commit()

    async def _fake(prompt):
        if "boom" in prompt:
            raise RuntimeError("llm down")
        return {"lost_caller": True, "prompt_fix": "f", "category": "gave-up-early"}

    monkeypatch.setattr(qa, "_llm_json", _fake)
    result = await qa.review_recent_failures(db_session, limit=15)
    # one blew up → skipped, the other still counted
    assert result["reviewed"] == 1


async def test_review_ignores_successful_outcomes(monkeypatch, db_session):
    practice, _ = await seed_practice(
        db_session, name="QA3", clerk_org_id="org_qa3", clerk_user_id="u_qa3"
    )
    db_session.add(_mk_call(practice.id, outcome="booked",
                            transcript=[{"role": "agent", "content": "won"}]))
    await db_session.commit()

    async def _fake(_prompt):
        pytest.fail("should not analyze a successful call")

    monkeypatch.setattr(qa, "_llm_json", _fake)
    result = await qa.review_recent_failures(db_session, limit=15)
    assert result["reviewed"] == 0
