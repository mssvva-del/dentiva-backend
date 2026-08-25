#!/usr/bin/env bash
# Container entrypoint for Railway / Render / any PaaS.
#
# Single source of truth for how the backend boots in the cloud:
#   1. Apply DB migrations (fail loudly if they error — better than a silently
#      broken app serving against an unmigrated schema). On Railway these have
#      already run as the pre-deploy step, so this is a no-op that returns at
#      once; it stays here so `docker run` alone still produces a working app.
#   2. Launch uvicorn bound to the platform-provided $PORT, falling back to 8000
#      for local Docker. Railway injects $PORT and health-checks exactly that
#      port; binding anything else makes the health-check hang forever.
set -euo pipefail

PORT="${PORT:-8000}"

echo "[start] applying database migrations (alembic upgrade head)..."
alembic upgrade head
echo "[start] migrations done. launching uvicorn on 0.0.0.0:${PORT}"

# --proxy-headers: behind Railway's edge proxy, request.client.host is the
# proxy's internal address unless uvicorn reads X-Forwarded-For. Without it the
# per-IP rate limiter put EVERY caller — all clinics' dashboards, every Retell
# tool call mid-conversation, every Stripe event — into one shared 120/minute
# bucket. Invisible with one clinic; a platform-wide 429 storm with a fleet.
# forwarded-allow-ips="*" is correct HERE because the app is only reachable
# through Railway's proxy; on a host with a public direct port it would let
# clients forge their IP.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" \
    --proxy-headers --forwarded-allow-ips="*"
