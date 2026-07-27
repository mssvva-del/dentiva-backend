"""Webhook-independent Retell call sync.

Retell web/test calls do not reliably fire call lifecycle webhooks, so a call
can complete on Retell's side yet leave nothing in our DB. This service pulls
recent calls straight from the Retell REST API and upserts them into ``calls``
so the dashboard stays current.

It runs two ways:
  * one-shot via :func:`sync_recent_calls` (also used by scripts/tests), and
  * as a periodic background loop via :func:`call_sync_loop`, started from the
    FastAPI lifespan when ``CALL_SYNC_ENABLED=true`` and a key is configured.

Upserts by ``retell_call_id`` (unique), so it is safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

import app.db as app_db
from app.config import get_settings
from app.db import set_tenant
from app.models.call import Call
from app.models.practice import Practice
from app.services.call_outcome import INFO_ONLY
from app.services.worker_lock import advisory_tick_lock

logger = logging.getLogger(__name__)

RETELL_BASE = "https://api.retellai.com"


# --------------------------------------------------------------------------- #
# Transforms — turn a Retell call object into DB-ready values
# --------------------------------------------------------------------------- #
def _ts_to_dt(ms: int | float | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _slim_transcript(call: dict) -> list[dict] | None:
    """Keep role + content only (drop word-level timing to stay lean)."""
    to = call.get("transcript_object")
    if isinstance(to, list) and to:
        slim = [
            {"role": t.get("role"), "content": t.get("content", "")}
            for t in to
            if t.get("content")
        ]
        return slim or None
    s = call.get("transcript")
    if isinstance(s, str) and s.strip():
        return [{"role": "raw", "content": s}]
    return None


def _status_for(call: dict) -> str:
    if call.get("call_status") == "error":
        return "missed"
    return "completed"


def _sentiment_label(call: dict) -> str | None:
    ca = call.get("call_analysis") or {}
    s = ca.get("user_sentiment")
    return s.lower() if isinstance(s, str) else None


# --------------------------------------------------------------------------- #
# Retell API
# --------------------------------------------------------------------------- #
async def fetch_recent_calls(
    api_key: str,
    agent_id: str | None,
    limit: int,
    *,
    all_agents: bool = False,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetch the most recent calls from Retell (newest first)."""
    body: dict = {"limit": limit, "sort_order": "descending"}
    if agent_id and not all_agents:
        body["filter_criteria"] = {"agent_id": [agent_id]}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        resp = await client.post(
            f"{RETELL_BASE}/v2/list-calls",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    if isinstance(data, list):
        return data
    calls = data.get("calls", data) if isinstance(data, dict) else []
    return calls if isinstance(calls, list) else []


# --------------------------------------------------------------------------- #
# Upsert + orchestration
# --------------------------------------------------------------------------- #
async def _upsert_call(session, practice_id, call: dict) -> str | None:
    """Insert or update one call. Returns 'inserted' | 'updated' | None (skip)."""
    cid = call.get("call_id")
    transcript = _slim_transcript(call)
    # Skip empty/errored calls with no conversation — not worth surfacing.
    if not cid or call.get("call_status") == "error" or not transcript:
        return None

    started = _ts_to_dt(call.get("start_timestamp")) or datetime.now(tz=UTC)
    ended = _ts_to_dt(call.get("end_timestamp"))
    dur_ms = call.get("duration_ms")
    duration = int(dur_ms / 1000) if dur_ms else None
    ctype = call.get("call_type") or "web_call"
    from_num = call.get("from_number") or ("web" if ctype == "web_call" else "unknown")
    to_num = call.get("to_number") or ("web" if ctype == "web_call" else "unknown")
    status = _status_for(call)
    recording = call.get("recording_url")
    sentiment = _sentiment_label(call)

    existing = (
        await session.execute(select(Call).where(Call.retell_call_id == cid))
    ).scalar_one_or_none()

    if existing is not None:
        existing.ended_at = ended
        existing.duration_seconds = duration
        existing.status = status
        existing.recording_path = recording
        existing.transcript_jsonb = transcript
        existing.patient_sentiment = sentiment
        return "updated"

    session.add(
        Call(
            practice_id=practice_id,
            retell_call_id=cid,
            direction="inbound",
            from_number=from_num,
            to_number=to_num,
            started_at=started,
            ended_at=ended,
            duration_seconds=duration,
            status=status,
            outcome=INFO_ONLY,
            recording_path=recording,
            transcript_jsonb=transcript,
            patient_sentiment=sentiment,
        )
    )
    return "inserted"


async def sync_recent_calls(
    limit: int | None = None,
    *,
    all_agents: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Pull recent Retell calls and upsert them. Returns a small result summary."""
    settings = get_settings()
    api_key = settings.retell_api_key
    if not api_key:
        logger.info("call-sync: no RETELL_API_KEY configured — skipping")
        return {"skipped": "no_api_key"}

    limit = limit or settings.call_sync_limit

    # Resolve the practice roster up front so each call attaches to the RIGHT
    # clinic (by its Retell agent id), never blanket-attach to the first practice
    # — that would leak one clinic's calls (PHI) into another's dashboard.
    async with app_db.async_session_factory() as session:
        practices = (
            await session.execute(select(Practice.id, Practice.retell_agent_id))
        ).all()
    if not practices:
        logger.warning("call-sync: no practice in DB — nothing to attach calls to")
        return {"error": "no_practice"}

    agent_to_practice = {agent_id: pid for pid, agent_id in practices if agent_id}
    # Single-clinic fallback: with exactly one practice, a call whose agent id we
    # can't match still belongs to it (demo/web calls fire no agent binding). With
    # 2+ clinics there is NO safe fallback — an unmatched call is skipped.
    single_practice_id = practices[0][0] if len(practices) == 1 else None
    # With 2+ clinics, filtering the fetch by one global agent id would starve the
    # others — pull every agent's calls and route each by its agent id.
    fetch_all = all_agents or len(practices) > 1

    calls = await fetch_recent_calls(
        api_key,
        settings.retell_agent_id or None,
        limit,
        all_agents=fetch_all,
        client=client,
    )

    # Bucket each call under its resolved practice; skip anything unplaceable.
    buckets: dict = defaultdict(list)
    unresolved = 0
    for call in calls:
        agent_id = call.get("agent_id")
        pid = agent_to_practice.get(agent_id) if agent_id else None
        if pid is None:
            pid = single_practice_id
        if pid is None:
            unresolved += 1
            continue
        buckets[pid].append(call)

    inserted = updated = skipped = 0
    # One session per practice bucket: set_tenant is connection-scoped and RLS is
    # FORCE'd, so mixing tenants in a single session risks a cross-tenant flush.
    for pid, pcalls in buckets.items():
        async with app_db.async_session_factory() as session:
            await set_tenant(session, pid)
            for call in pcalls:
                result = await _upsert_call(session, pid, call)
                if result == "inserted":
                    inserted += 1
                elif result == "updated":
                    updated += 1
                else:
                    skipped += 1
            await session.commit()

    summary = {
        "fetched": len(calls),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "unresolved": unresolved,
    }
    return summary


# --------------------------------------------------------------------------- #
# Background loop (started from the FastAPI lifespan)
# --------------------------------------------------------------------------- #
async def call_sync_loop() -> None:
    """Run :func:`sync_recent_calls` forever on a fixed interval."""
    interval = get_settings().call_sync_interval_seconds
    logger.info("call-sync loop started (every %ss)", interval)
    while True:
        try:
            # Only the instance that wins the advisory lock runs this tick — on
            # 2+ instances the others skip, so calls aren't fetched/upserted twice.
            async with advisory_tick_lock("call_sync") as leader:
                if leader:
                    result = await sync_recent_calls()
                    logger.info("call-sync: %s", result)
        except asyncio.CancelledError:
            logger.info("call-sync loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("call-sync iteration failed; will retry next tick")
        await asyncio.sleep(interval)
