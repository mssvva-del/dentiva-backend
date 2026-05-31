#!/usr/bin/env bash
#
# One-shot: pull completed Retell calls into the live (Railway) DB so they show
# up in the dashboard. Run this RIGHT AFTER a live demo web call.
#
# Why: Retell web/test calls don't reliably fire call lifecycle webhooks, so the
# call won't appear on its own. This pulls it straight from the Retell API.
#
# Usage:
#   bash scripts/sync_calls.sh            # last 20 calls for the demo agent
#   bash scripts/sync_calls.sh 5          # last 5 calls
#
# Reads:
#   DB URL   -> _docs/_railway_db.txt   (the *.proxy.rlwy.net public URL)
#   API key  -> dentiva-voice/.env      (RETELL_API_KEY + RETELL_AGENT_ID)
#
# Safe to re-run: upserts by retell_call_id (no duplicates).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIMIT="${1:-20}"

PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "==> Syncing last ${LIMIT} Retell call(s) into the live dashboard DB..."
( cd "$ROOT" && "$PY" scripts/sync_calls_from_retell.py --limit "$LIMIT" )
echo "==> Done. Refresh the dashboard (Cmd+R) — the call + transcript appear under Front Desk."
