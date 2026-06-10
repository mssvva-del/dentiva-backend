"""RLS on calls + bookings (close PHI tenant-isolation gap)

Audit finding (CURRENT_STATE_AUDIT.md §1): `calls` (transcripts + phone numbers =
PHI) and `bookings` carried practice_id but had NO row-level security — the only
isolation was app-layer filtering. This adds the DB-level backstop so a single
forgotten `practice_id` filter can't leak one clinic's calls/bookings to another.

Same FORCE-RLS + tenant_isolation policy as patients/callback_requests. The Retell
webhook + call-sync set the tenant GUC before touching these tables (cross-tenant
ingestion resolves the practice from the agent_id first).

Revision ID: r3m4n5o6p7q8
Revises: q2l3m4n5o6p7
Create Date: 2026-06-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'r3m4n5o6p7q8'
down_revision: str | None = 'q2l3m4n5o6p7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("calls", "bookings")


def upgrade() -> None:
    for t in _TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {t}
              USING (practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid)
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO dentiva_app")

    # Trusted ROUTING lookup: the Retell webhook receives a retell_call_id and
    # must discover which practice it belongs to BEFORE it can bind the tenant —
    # a chicken-and-egg with RLS. This SECURITY DEFINER function (owned by the
    # superuser that runs the migration) resolves ONLY the practice_id, bypassing
    # RLS for that single non-PHI field. It exposes no patient data, so it's a
    # safe, minimal hole purely for tenant routing.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dentiva_practice_for_retell_call(p_call_id text)
        RETURNS uuid
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT practice_id FROM calls WHERE retell_call_id = p_call_id LIMIT 1;
        $$;
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION dentiva_practice_for_retell_call(text) TO dentiva_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS dentiva_practice_for_retell_call(text)")
    for t in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
