"""Reactivation Engine schema + patients.preferred_language (Phase 1, block 1)

Adds the bilingual language preference to patients and the three core
reactivation tables (campaigns / targets / touches). The reactivation tables are
tenant-isolated (FORCE RLS) exactly like patients/bookings — they hold a clinic's
dormant-patient recall list, which is PHI-adjacent and must never cross tenants.

Revision ID: v7q8r9s0t1u2
Revises: u6p7q8r9s0t1
Create Date: 2026-06-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'v7q8r9s0t1u2'
down_revision: str | None = 'u6p7q8r9s0t1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("reactivation_campaigns", "reactivation_targets", "reactivation_touches")


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _rls(table: str) -> None:
    """Enable + force tenant-isolation RLS on a per-practice table (same policy
    as patients/bookings) and grant the app role DML."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
          USING (practice_id = NULLIF(current_setting('app.current_practice_id', true), '')::uuid)
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO dentiva_app")


def upgrade() -> None:
    # Bilingual: patient language preference (EN/ES). Plain text (not PHI) so
    # campaigns can segment by language in SQL. Backfills existing rows to 'en'.
    op.add_column(
        "patients",
        sa.Column("preferred_language", sa.Text(), nullable=False, server_default="en"),
    )

    op.create_table(
        "reactivation_campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("practice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("segment", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["practice_id"], ["practices.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reactivation_campaigns_practice", "reactivation_campaigns", ["practice_id"]
    )
    _rls("reactivation_campaigns")

    op.create_table(
        "reactivation_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("practice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment", sa.Text(), nullable=False),
        sa.Column("value_score", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        _ts("next_touch_at", nullable=True),
        sa.Column("touches_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["practice_id"], ["practices.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["reactivation_campaigns.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "patient_id", name="uq_target_campaign_patient"),
    )
    op.create_index(
        "ix_reactivation_targets_practice", "reactivation_targets", ["practice_id"]
    )
    # The scheduler queries "running campaign targets due for a touch, by priority".
    op.create_index(
        "ix_reactivation_targets_queue",
        "reactivation_targets",
        ["practice_id", "status", "next_touch_at"],
    )
    _rls("reactivation_targets")

    op.create_table(
        "reactivation_touches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("practice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False, server_default="en"),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        _ts("occurred_at", nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["practice_id"], ["practices.id"]),
        sa.ForeignKeyConstraint(["target_id"], ["reactivation_targets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reactivation_touches_target", "reactivation_touches", ["target_id"]
    )
    _rls("reactivation_touches")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("reactivation_touches")
    op.drop_table("reactivation_targets")
    op.drop_table("reactivation_campaigns")
    op.drop_column("patients", "preferred_language")
