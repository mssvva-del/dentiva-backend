#!/usr/bin/env bash
# Apply migrations, retrying past the release we are replacing.
#
# On a zero-downtime platform the PREVIOUS release keeps serving traffic until
# the new one is healthy. A migration needing ACCESS EXCLUSIVE therefore has to
# take its lock while another process is actively querying the same tables — and
# a lock request that waits also QUEUES every new query behind it, so waiting is
# not just slow for us, it stalls the live clinic.
#
# The migration sets a short lock_timeout, so an attempt gives up in seconds
# rather than blocking anyone. That turns the problem into "try again in a gap
# between requests", which is what this loop does. Each attempt is a fresh
# transaction: Alembic runs a migration in one transaction, so a failed attempt
# is rolled back whole and there is nothing to clean up before retrying.
set -uo pipefail

ATTEMPTS="${MIGRATE_ATTEMPTS:-8}"
WAIT="${MIGRATE_WAIT_SECONDS:-15}"

for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "[migrate] attempt ${attempt}/${ATTEMPTS}"
  if alembic upgrade head; then
    echo "[migrate] schema is at head"
    exit 0
  fi
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    echo "[migrate] attempt failed — retrying in ${WAIT}s"
    sleep "$WAIT"
  fi
done

cat <<'EOF'

[migrate] every attempt failed.

If the errors above say "canceling statement due to lock timeout", the previous
release never left a gap long enough to take the lock. Scale the service to zero
replicas, run this once against an idle database, then scale back up — that is
the only way to get an exclusive lock while a release is live.

Any other error is a real migration bug: read the last one, it is unabridged.
EOF
exit 1
