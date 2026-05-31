#!/usr/bin/env bash
# Container entrypoint for Railway / Render / any PaaS.
#
# Single source of truth for how the backend boots in the cloud:
#   1. Apply DB migrations (fail loudly if they error — better than a silently
#      broken app serving against an unmigrated schema).
#   2. Launch uvicorn bound to the platform-provided $PORT, falling back to 8000
#      for local Docker. Railway injects $PORT and health-checks exactly that
#      port; binding anything else makes the health-check hang forever.
set -euo pipefail

PORT="${PORT:-8000}"

echo "[start] applying database migrations (alembic upgrade head)..."
alembic upgrade head
echo "[start] migrations done. launching uvicorn on 0.0.0.0:${PORT}"

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
