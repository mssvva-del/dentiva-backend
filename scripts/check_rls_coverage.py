#!/usr/bin/env python
"""Tenant-isolation CI gate (audit Risk 5).

Run AFTER `alembic upgrade head` against a throwaway DB. Asserts that every
PHI/tenant table that MUST be row-level-security protected still has FORCE RLS —
so a future migration can't silently drop the DB-level backstop that keeps one
clinic's data from leaking to another.

  * FAIL (exit 1) if any REQUIRED table lacks forced RLS — a regression guard
    with zero false positives.
  * WARN (exit 0, printed) for any OTHER table that has a ``practice_id`` column
    but no forced RLS — advisory, so a newly-added tenant table that forgot its
    RLS migration gets a human's attention without blocking the build on a
    judgement call (some practice_id tables are intentionally non-RLS: users,
    billing, audit_logs — read cross-tenant by the admin path).

Connects via DATABASE_URL_SYNC (the same URL Alembic uses).
"""

from __future__ import annotations

import os
import sys

import psycopg2

# Tables whose tenant isolation is enforced at the DB layer and must stay that
# way. Losing RLS on any of these is a security regression, not a style nit.
REQUIRED_RLS = {
    "patients",
    "calls",
    "bookings",
    "callback_requests",
    "waitlist_entries",
    "invitations",
}


def _libpq_url() -> str:
    url = os.environ.get("DATABASE_URL_SYNC", "")
    if not url:
        sys.exit("DATABASE_URL_SYNC not set")
    # psycopg2 wants a plain libpq URL, not the SQLAlchemy '+psycopg2' dialect.
    return url.replace("postgresql+psycopg2://", "postgresql://")


def main() -> int:
    conn = psycopg2.connect(_libpq_url())
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.relname, c.relforcerowsecurity
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        WHERE c.relkind = 'r'
          AND EXISTS (
              SELECT 1 FROM information_schema.columns col
              WHERE col.table_schema = 'public'
                AND col.table_name = c.relname
                AND col.column_name = 'practice_id'
          )
        ORDER BY c.relname
        """
    )
    forced = {name: bool(force) for name, force in cur.fetchall()}
    cur.close()
    conn.close()

    missing_required = sorted(t for t in REQUIRED_RLS if not forced.get(t, False))
    advisory = sorted(
        t for t, f in forced.items() if not f and t not in REQUIRED_RLS
    )

    print("RLS coverage of tables carrying practice_id (forced = DB-enforced):")
    for name in sorted(forced):
        flag = "FORCED" if forced[name] else "none"
        print(f"  {name:24} {flag}")

    if advisory:
        print(
            "\nADVISORY: these carry practice_id but have no forced RLS — confirm "
            "they are intentionally cross-tenant (users/billing/audit) and not a "
            "forgotten RLS migration:\n  " + ", ".join(advisory)
        )

    if missing_required:
        print(
            "\nFAIL: required tenant tables lost forced RLS: "
            + ", ".join(missing_required)
            + "\nA tenant table must keep DB-level RLS — add it back in a migration."
        )
        return 1

    print("\nOK: all required tenant tables have forced RLS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
