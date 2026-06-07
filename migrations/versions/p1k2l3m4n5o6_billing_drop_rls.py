"""Drop RLS on billing tables for cross-tenant admin reads (Phase E)

Subscriptions / invoices / usage_records are NOT patient PHI, and the Dentiva
admin panel must read them cross-tenant (revenue, usage, margin rollups). Under
FORCE RLS the app role sees nothing without a per-practice GUC, which blocks
aggregates. We drop RLS on these three and rely on explicit practice_id filtering
in the clinic billing API (already in place) — consistent with users/practices,
which are also non-RLS + app-filtered. PHI tables keep their RLS; admin reads a
specific clinic's PHI via set_tenant on a separate, audited code path.

Revision ID: p1k2l3m4n5o6
Revises: o0j1k2l3m4n5
Create Date: 2026-06-07
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'p1k2l3m4n5o6'
down_revision: str | None = 'o0j1k2l3m4n5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("subscriptions", "invoices", "usage_records")


def upgrade() -> None:
    for t in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for t in _TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {t}
              USING (practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid)
            """
        )
