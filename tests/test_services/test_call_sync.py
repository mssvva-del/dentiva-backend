"""Tests for the webhook-independent Retell call sync service."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from app.models.call import Call
from app.services import call_sync
from tests.conftest import seed_practice


# --------------------------------------------------------------------------- #
# Pure transform tests
# --------------------------------------------------------------------------- #
def test_ts_to_dt_handles_none_and_ms():
    assert call_sync._ts_to_dt(None) is None
    assert call_sync._ts_to_dt(0) is None
    dt = call_sync._ts_to_dt(1_700_000_000_000)
    assert dt is not None and dt.year == 2023


def test_slim_transcript_prefers_object_then_string():
    call = {
        "transcript_object": [
            {"role": "agent", "content": "Hi", "words": [{"word": "Hi"}]},
            {"role": "user", "content": ""},  # dropped (empty)
            {"role": "user", "content": "I need a cleaning"},
        ]
    }
    slim = call_sync._slim_transcript(call)
    assert slim == [
        {"role": "agent", "content": "Hi"},
        {"role": "user", "content": "I need a cleaning"},
    ]

    assert call_sync._slim_transcript({"transcript": "raw text"}) == [
        {"role": "raw", "content": "raw text"}
    ]
    assert call_sync._slim_transcript({}) is None


def test_status_and_sentiment():
    assert call_sync._status_for({"call_status": "error"}) == "missed"
    assert call_sync._status_for({"call_status": "ended"}) == "completed"
    positive = call_sync._sentiment_label({"call_analysis": {"user_sentiment": "Positive"}})
    assert positive == "positive"
    assert call_sync._sentiment_label({}) is None


# --------------------------------------------------------------------------- #
# sync_recent_calls orchestration
# --------------------------------------------------------------------------- #
def _patch_settings(monkeypatch, *, api_key="test-key", agent="agent_demo", limit=20):
    monkeypatch.setattr(
        call_sync,
        "get_settings",
        lambda: SimpleNamespace(
            retell_api_key=api_key,
            retell_agent_id=agent,
            call_sync_limit=limit,
        ),
    )


def _patch_fetch(monkeypatch, calls):
    async def _fake_fetch(*_a, **_kw):
        return calls

    monkeypatch.setattr(call_sync, "fetch_recent_calls", _fake_fetch)


def _good_call(cid="call_1"):
    return {
        "call_id": cid,
        "call_type": "web_call",
        "call_status": "ended",
        "start_timestamp": 1_700_000_000_000,
        "end_timestamp": 1_700_000_100_000,
        "duration_ms": 100_000,
        "transcript_object": [
            {"role": "agent", "content": "Thanks for calling Dentiva."},
            {"role": "user", "content": "I'd like to book a cleaning."},
        ],
        "recording_url": "https://example.com/rec.wav",
        "call_analysis": {"user_sentiment": "Positive"},
    }


async def test_sync_inserts_new_calls(db_session, monkeypatch):
    practice, _ = await seed_practice(
        db_session, name="Sync One", clerk_org_id="org_s1", clerk_user_id="user_s1"
    )
    _patch_settings(monkeypatch)
    _patch_fetch(monkeypatch, [_good_call("call_sync_a")])

    result = await call_sync.sync_recent_calls()
    assert result == {"fetched": 1, "inserted": 1, "updated": 0, "skipped": 0}

    row = (
        await db_session.execute(
            select(Call).where(Call.retell_call_id == "call_sync_a")
        )
    ).scalar_one()
    assert row.practice_id == practice.id
    assert row.status == "completed"
    assert row.duration_seconds == 100
    assert row.patient_sentiment == "positive"
    assert row.direction == "inbound"
    assert row.outcome == "info_only"
    assert isinstance(row.transcript_jsonb, list) and len(row.transcript_jsonb) == 2


async def test_sync_skips_errored_and_empty_calls(db_session, monkeypatch):
    await seed_practice(
        db_session, name="Sync Two", clerk_org_id="org_s2", clerk_user_id="user_s2"
    )
    _patch_settings(monkeypatch)
    _patch_fetch(
        monkeypatch,
        [
            _good_call("call_keep"),
            {"call_id": "call_err", "call_status": "error",
             "transcript_object": [{"role": "agent", "content": "x"}]},
            {"call_id": "call_empty", "call_status": "ended"},  # no transcript
        ],
    )

    result = await call_sync.sync_recent_calls()
    assert result["inserted"] == 1
    assert result["skipped"] == 2

    rows = (await db_session.execute(select(Call.retell_call_id))).scalars().all()
    assert "call_keep" in rows
    assert "call_err" not in rows
    assert "call_empty" not in rows


async def test_sync_is_idempotent(db_session, monkeypatch):
    await seed_practice(
        db_session, name="Sync Three", clerk_org_id="org_s3", clerk_user_id="user_s3"
    )
    _patch_settings(monkeypatch)
    _patch_fetch(monkeypatch, [_good_call("call_dupe")])

    first = await call_sync.sync_recent_calls()
    second = await call_sync.sync_recent_calls()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["updated"] == 1

    count = (
        await db_session.execute(
            select(Call).where(Call.retell_call_id == "call_dupe")
        )
    ).scalars().all()
    assert len(count) == 1  # no duplicate row


async def test_sync_skips_without_api_key(db_session, monkeypatch):
    await seed_practice(
        db_session, name="Sync Four", clerk_org_id="org_s4", clerk_user_id="user_s4"
    )
    _patch_settings(monkeypatch, api_key="")
    # fetch should never be called; make it explode if it is.
    def _boom(*_a, **_kw):
        raise AssertionError("fetch_recent_calls must not run without a key")

    monkeypatch.setattr(call_sync, "fetch_recent_calls", _boom)

    result = await call_sync.sync_recent_calls()
    assert result == {"skipped": "no_api_key"}
