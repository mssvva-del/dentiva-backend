#!/usr/bin/env bash
#
# One-shot Railway seeder. Reads the PUBLIC Postgres URL from
# _docs/_railway_db.txt (private, gitignored), seeds the demo data, and
# provisions the dashboard login user — all against the Railway database.
#
# Usage:
#   bash scripts/seed_railway.sh                 # seed + provision mssvva@gmail.com
#   bash scripts/seed_railway.sh you@example.com # seed + provision a different email
#
# Prereqs:
#   1) _docs/_railway_db.txt contains the Railway DATABASE_PUBLIC_URL
#      (the *.proxy.rlwy.net one — NOT the *.railway.internal one).
#      The file may contain just the URL, or a KEY=value line; both work.
#   2) .env has CLERK_SECRET_KEY (used to look up the Clerk user by email).
#
# Safe to re-run: seed_demo.py clears its own demo rows, provision is idempotent.

set -euo pipefail

# Resolve repo root (this script lives in <root>/scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DB_FILE="${ROOT}/../_docs/_railway_db.txt"
EMAIL="${1:-mssvva@gmail.com}"

PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "==> Dentiva Railway seeder"

if [ ! -f "$DB_FILE" ]; then
  echo "ERROR: $DB_FILE not found." >&2
  echo "       Paste the Railway DATABASE_PUBLIC_URL into that file first" >&2
  echo "       (see _docs/RAILWAY_SEED.md)." >&2
  exit 1
fi

# Extract the first postgres URL we can find in the file. Handles plain URL,
# KEY=url, surrounding quotes, and stray whitespace/blank lines.
RAW_URL="$(grep -Eo 'postgres(ql)?(\+[a-z0-9]+)?://[^[:space:]"'"'"']+' "$DB_FILE" | head -n1 || true)"

if [ -z "$RAW_URL" ]; then
  echo "ERROR: No postgres URL found in $DB_FILE." >&2
  echo "       Expected something like postgresql://...@...proxy.rlwy.net:PORT/railway" >&2
  exit 1
fi

# Safety: warn loudly if this looks like the INTERNAL url (won't reach from laptop).
if printf '%s' "$RAW_URL" | grep -q 'railway.internal'; then
  echo "ERROR: That looks like the INTERNAL url (*.railway.internal)." >&2
  echo "       From your laptop you need the PUBLIC one (*.proxy.rlwy.net)." >&2
  echo "       In Railway: Postgres service -> Variables -> DATABASE_PUBLIC_URL." >&2
  exit 1
fi

# Mask host for a friendly, non-leaky confirmation line.
MASKED="$(printf '%s' "$RAW_URL" | sed -E 's#(://)[^@]*@#\1***:***@#')"
echo "==> Target DB: ${MASKED}"

export DATABASE_URL="$RAW_URL"

# CRITICAL: PII (patient names) is Fernet-encrypted with ENCRYPTION_KEY. The
# PRODUCTION backend on Railway decrypts with ITS key, so the seed must encrypt
# with the SAME key — otherwise endpoints that read names (/api/calls,
# /api/patients) crash with InvalidToken while count-only endpoints still work.
# Pull the prod app vars from the Railway paste block so the data matches prod.
PASTE_ENV="${ROOT}/../_docs/_railway_paste.env"
if [ -f "$PASTE_ENV" ]; then
  # NOTE: use grep|cut, NOT `IFS='=' read` — the Fernet key ends with '=', and
  # the read-loop strips that trailing delimiter (43-char key -> "Incorrect
  # padding"). cut -f2- keeps everything after the first '=' verbatim.
  _paste_get() { grep "^$1=" "$PASTE_ENV" | head -n1 | cut -d= -f2-; }
  ENCRYPTION_KEY="$(_paste_get ENCRYPTION_KEY)"; export ENCRYPTION_KEY
  CLERK_SECRET_KEY="$(_paste_get CLERK_SECRET_KEY)"; export CLERK_SECRET_KEY
  GROQ_API_KEY="$(_paste_get GROQ_API_KEY)"; export GROQ_API_KEY
  PMS_ADAPTER="$(_paste_get PMS_ADAPTER)"; export PMS_ADAPTER
  if [ -n "${ENCRYPTION_KEY:-}" ]; then
    echo "==> Using PROD ENCRYPTION_KEY from _railway_paste.env (len ${#ENCRYPTION_KEY}; PII will match prod)."
  fi
else
  echo "WARN: $PASTE_ENV not found — seeding with LOCAL ENCRYPTION_KEY." >&2
  echo "      If patient names fail to load in prod, that's the key mismatch." >&2
fi

echo
echo "==> [1/3] Clearing previous demo rows (bookings, calls, patients)..."
"$PY" - <<'PY'
import os, psycopg2
url = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+psycopg2", "")
conn = psycopg2.connect(url); conn.autocommit = True
cur = conn.cursor()
# FK-safe order; practices/users kept so the provisioned login survives.
# waitlist_entries + callback_requests FK to calls/patients, so clear first.
for t in (
    "waitlist_entries",
    "callback_requests",
    "bookings",
    "calls",
    "audit_logs",
    "patients",
):
    cur.execute(f"DELETE FROM {t}")
    print(f"  cleared {t}")
cur.close(); conn.close()
PY

echo
echo "==> [2/3] Seeding demo data (scripts/seed_demo.py)..."
( cd "$ROOT" && "$PY" scripts/seed_demo.py )

echo
echo "==> [3/3] Provisioning dashboard user: ${EMAIL}"
( cd "$ROOT" && "$PY" scripts/provision_user.py --email "$EMAIL" --role owner )

echo
echo "==> Done. Open the Vercel dashboard and log in as ${EMAIL}."
echo "    Tables should now show calls, bookings, and recall patients."
