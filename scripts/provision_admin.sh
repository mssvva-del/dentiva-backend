#!/usr/bin/env bash
#
# One-shot: make an email a Dentiva INTERNAL admin on the PRODUCTION (Railway) DB.
#
# Usage:
#   bash scripts/provision_admin.sh you@example.com              # role: super_admin
#   bash scripts/provision_admin.sh you@example.com finance      # custom role
#
# Prereqs (same as seed_railway.sh):
#   1) _docs/_railway_db.txt (in dentiva-starter/_docs/, gitignored) contains the
#      Railway DATABASE_PUBLIC_URL — the *.proxy.rlwy.net one, NOT *.railway.internal.
#      In Railway: Postgres service -> Variables -> DATABASE_PUBLIC_URL.
#   2) dentiva-backend/.env has CLERK_SECRET_KEY (used to find the Clerk user by
#      email — the person must have SIGNED UP in the dashboard first).
#
# Idempotent: re-running updates the role for the same user.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DB_FILE="${ROOT}/../_docs/_railway_db.txt"
EMAIL="${1:?Usage: bash scripts/provision_admin.sh you@example.com [role]}"
ROLE="${2:-super_admin}"

PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "==> Dentiva admin provisioner (${EMAIL} -> ${ROLE})"

if [ ! -f "$DB_FILE" ]; then
  echo "ERROR: $DB_FILE not found." >&2
  echo "       Create it and paste the Railway DATABASE_PUBLIC_URL inside:" >&2
  echo "       Railway -> Postgres service -> Variables -> DATABASE_PUBLIC_URL" >&2
  exit 1
fi

# First postgres URL in the file (plain URL, KEY=url, quotes — all fine).
RAW_URL="$(grep -Eo 'postgres(ql)?(\+[a-z0-9]+)?://[^[:space:]"'"'"']+' "$DB_FILE" | head -n1 || true)"

if [ -z "$RAW_URL" ]; then
  echo "ERROR: No postgres URL found in $DB_FILE." >&2
  echo "       Expected: postgresql://...@...proxy.rlwy.net:PORT/railway" >&2
  exit 1
fi

if printf '%s' "$RAW_URL" | grep -q 'railway.internal'; then
  echo "ERROR: That is the INTERNAL url (*.railway.internal) — unreachable from a laptop." >&2
  echo "       Use the PUBLIC one: Postgres service -> Variables -> DATABASE_PUBLIC_URL." >&2
  exit 1
fi

# provision_internal.py uses the SYNC engine — normalize the driver prefix.
SYNC_URL="$(printf '%s' "$RAW_URL" | sed -E 's#^postgres(ql)?(\+[a-z0-9]+)?://#postgresql+psycopg2://#')"

MASKED="$(printf '%s' "$SYNC_URL" | sed -E 's#(://)[^@]*@#\1***:***@#')"
echo "==> Target DB: ${MASKED}"

cd "$ROOT"
DATABASE_URL_SYNC="$SYNC_URL" "$PY" scripts/provision_internal.py \
  --email "$EMAIL" --role "$ROLE"

echo "==> Done. Open the admin panel: https://dentiva-dashboard.vercel.app/admin"
