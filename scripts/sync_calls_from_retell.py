#!/usr/bin/env python3
"""Pull completed Retell calls into the Dentiva DB (webhook-independent).

Why this exists
---------------
Retell web (test) calls do NOT reliably deliver call_started/call_ended
webhooks to the agent webhook_url, and custom-tool invocations use a different
payload shape than our webhook handler expects. So a live dashboard test call
can succeed end-to-end on Retell's side yet leave nothing in our DB.

This script is the robust fallback for demos: after a call, it reads the call
list straight from the Retell REST API and upserts each one into `calls`
(transcript, recording, duration, status, sentiment). The dashboard then shows
the real call + transcript regardless of whether any webhook fired.

Uses ONLY the stdlib (urllib) for the Retell API + psycopg2 for the DB, so it
runs in the backend venv with no extra installs.

Usage
-----
  # Defaults: DB from _docs/_railway_db.txt, key + agent from dentiva-voice/.env
  python scripts/sync_calls_from_retell.py
  python scripts/sync_calls_from_retell.py --limit 20
  python scripts/sync_calls_from_retell.py --all-agents   # ignore agent filter

Safe to re-run: upserts by retell_call_id (no duplicates).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]          # dentiva-backend/
REPO = ROOT.parent                                   # dentiva-starter/
VOICE_ENV = REPO / "dentiva-voice" / ".env"

RETELL_BASE = "https://api.retellai.com"


# --------------------------------------------------------------------------- #
# Small helpers to read config without extra deps
# --------------------------------------------------------------------------- #
def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _resolve_db_url(cli: str | None) -> str:
    if cli:
        return cli
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    sys.exit("ERROR: no DB URL (pass --db or set DATABASE_URL)")


def _retell_post(path: str, key: str, body: dict) -> object:
    req = urllib.request.Request(
        f"{RETELL_BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# --------------------------------------------------------------------------- #
# Transform a Retell call object into a DB row
# --------------------------------------------------------------------------- #
def _ts_to_dt(ms: int | float | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _slim_transcript(call: dict) -> list | None:
    to = call.get("transcript_object")
    if isinstance(to, list) and to:
        # Keep role + content only (drop word-level timing to stay lean).
        return [
            {"role": t.get("role"), "content": t.get("content", "")}
            for t in to
            if t.get("content")
        ]
    s = call.get("transcript")
    if isinstance(s, str) and s.strip():
        return [{"role": "raw", "content": s}]
    return None


def _status_for(call: dict) -> str:
    cs = call.get("call_status")
    disc = call.get("disconnection_reason") or ""
    if cs == "error":
        return "missed"
    if disc in ("user_hangup", "agent_hangup", "call_transfer", ""):
        return "completed"
    return "completed"


def _sentiment_label(call: dict) -> str | None:
    ca = call.get("call_analysis") or {}
    s = ca.get("user_sentiment")
    return s.lower() if isinstance(s, str) else None


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--db", default=None, help="Postgres URL (overrides file/env)")
    ap.add_argument("--key", default=None, help="Retell API key (overrides voice/.env)")
    ap.add_argument("--agent", default=None, help="Retell agent_id filter")
    ap.add_argument("--all-agents", action="store_true", help="ignore agent filter")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    voice_env = _read_env_file(VOICE_ENV)
    key = args.key or os.environ.get("RETELL_API_KEY") or voice_env.get("RETELL_API_KEY")
    if not key:
        sys.exit("ERROR: no RETELL_API_KEY (pass --key or set it in dentiva-voice/.env)")
    agent_id = args.agent or voice_env.get("RETELL_AGENT_ID")

    db_url = _resolve_db_url(args.db).replace("+asyncpg", "").replace("+psycopg2", "")

    # --- fetch calls from Retell -----------------------------------------
    body: dict = {"limit": args.limit, "sort_order": "descending"}
    if agent_id and not args.all_agents:
        body["filter_criteria"] = {"agent_id": [agent_id]}
    resp = _retell_post("/v2/list-calls", key, body)
    calls = resp if isinstance(resp, list) else resp.get("calls", resp)
    if not isinstance(calls, list):
        sys.exit(f"Unexpected Retell response shape: {type(resp)}")
    print(f"==> Retell returned {len(calls)} call(s) "
          f"(agent={'ALL' if args.all_agents else agent_id})")

    # --- DB connect + resolve practice -----------------------------------
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT id FROM practices ORDER BY created_at LIMIT 1")
    row = cur.fetchone()
    if not row:
        sys.exit("ERROR: no practice in DB — seed first.")
    practice_id = row[0]

    upserted = skipped = 0
    for call in calls:
        cid = call.get("call_id")
        transcript = _slim_transcript(call)
        # Skip empty/errored calls with no conversation — not demo-worthy.
        if not cid or call.get("call_status") == "error" or not transcript:
            skipped += 1
            continue

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

        if args.dry_run:
            print(f"  [dry] {cid} status={status} dur={duration}s turns={len(transcript)} "
                  f"sentiment={sentiment}")
            upserted += 1
            continue

        cur.execute(
            """
            INSERT INTO calls (id, practice_id, retell_call_id, direction,
                from_number, to_number, started_at, ended_at, duration_seconds,
                status, outcome, recording_path, transcript_jsonb,
                patient_sentiment, created_at, updated_at)
            VALUES (%(id)s, %(practice_id)s, %(rcid)s, 'inbound',
                %(from_num)s, %(to_num)s, %(started)s, %(ended)s, %(duration)s,
                %(status)s, 'info_only', %(recording)s, %(transcript)s,
                %(sentiment)s, now(), now())
            ON CONFLICT (retell_call_id) DO UPDATE SET
                ended_at = EXCLUDED.ended_at,
                duration_seconds = EXCLUDED.duration_seconds,
                status = EXCLUDED.status,
                recording_path = EXCLUDED.recording_path,
                transcript_jsonb = EXCLUDED.transcript_jsonb,
                patient_sentiment = EXCLUDED.patient_sentiment,
                updated_at = now()
            """,
            {
                "id": str(uuid.uuid4()),
                "practice_id": practice_id,
                "rcid": cid,
                "from_num": from_num,
                "to_num": to_num,
                "started": started,
                "ended": ended,
                "duration": duration,
                "status": status,
                "recording": recording,
                "transcript": json.dumps(transcript),
                "sentiment": sentiment,
            },
        )
        upserted += 1
        print(f"  upserted {cid}  status={status} dur={duration}s turns={len(transcript)}")

    if not args.dry_run:
        conn.commit()
    cur.close()
    conn.close()
    print(f"==> Done. upserted={upserted} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
