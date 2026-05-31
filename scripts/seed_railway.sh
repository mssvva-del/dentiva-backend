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

echo
echo "==> [1/2] Seeding demo data (scripts/seed_demo.py)..."
( cd "$ROOT" && "$PY" scripts/seed_demo.py )

echo
echo "==> [2/2] Provisioning dashboard user: ${EMAIL}"
( cd "$ROOT" && "$PY" scripts/provision_user.py --email "$EMAIL" --role owner )

echo
echo "==> Done. Open the Vercel dashboard and log in as ${EMAIL}."
echo "    Tables should now show calls, bookings, and recall patients."
