"""pricing_plans + platform_settings — editable price grid + referral knobs (ADM6)

Public MARKETING pricing the site reads via GET /api/pricing and staff edit in the
admin panel — SEPARATE from billing/plans.py (Stripe metering). Seeded with the
2026-07 competitive-audit grid ($239/$379/$579/$849, annual -16%, fair-use
soft-caps). Business config (referral reward + invitee/annual discount) lives in a
singleton platform_settings row. Neither table is tenant-scoped → no RLS; the app
role gets DML so public reads + admin edits work.

Revision ID: c4x5y6z7a8b9
Revises: b3w4x5y6z7a8
Create Date: 2026-07-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = 'c4x5y6z7a8b9'
down_revision: str | None = 'b3w4x5y6z7a8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Seed grid — 2026-07 competitive audit. annual_monthly_cents = round(monthly*0.84)
# to whole dollars. All editable from the admin panel afterwards.
_PLANS = [
    # key, name, monthly, annual/mo, soft_cap, overage¢, react_level, per_loc, highlight, sort
    ("after_hours", "After-Hours", 23900, 20100, 1500, 18, "basic", False, False, 0),
    ("full_time", "Full-Time", 37900, 31800, 2500, 15, "full", False, True, 1),
    ("growth", "Growth", 57900, 48600, 4000, 13, "full", False, False, 2),
    ("multi", "Multi-Location", 84900, 71300, 3000, 11, "full", True, False, 3),
]


def upgrade() -> None:
    op.create_table(
        "pricing_plans",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("plan_key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("monthly_cents", sa.Integer(), nullable=False),
        sa.Column("annual_monthly_cents", sa.Integer(), nullable=False),
        sa.Column("soft_cap_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overage_cents_per_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reactivation_level", sa.Text(), nullable=False, server_default="none"),
        sa.Column("per_location", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("highlight", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table(
        "platform_settings",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("referrer_reward_months", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("invitee_discount_percent", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("annual_discount_percent", sa.Integer(), nullable=False, server_default="16"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON pricing_plans TO dentiva_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON platform_settings TO dentiva_app"
    )

    plans = sa.table(
        "pricing_plans",
        sa.column("id", PGUUID(as_uuid=True)),
        sa.column("plan_key", sa.Text),
        sa.column("name", sa.Text),
        sa.column("monthly_cents", sa.Integer),
        sa.column("annual_monthly_cents", sa.Integer),
        sa.column("soft_cap_minutes", sa.Integer),
        sa.column("overage_cents_per_min", sa.Integer),
        sa.column("reactivation_level", sa.Text),
        sa.column("per_location", sa.Boolean),
        sa.column("highlight", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    import uuid
    op.bulk_insert(plans, [
        {
            "id": uuid.uuid4(), "plan_key": k, "name": n, "monthly_cents": m,
            "annual_monthly_cents": am, "soft_cap_minutes": cap,
            "overage_cents_per_min": ov, "reactivation_level": rl,
            "per_location": pl, "highlight": hl, "sort_order": so,
        }
        for (k, n, m, am, cap, ov, rl, pl, hl, so) in _PLANS
    ])

    settings = sa.table(
        "platform_settings",
        sa.column("id", PGUUID(as_uuid=True)),
        sa.column("referrer_reward_months", sa.Integer),
        sa.column("invitee_discount_percent", sa.Integer),
        sa.column("annual_discount_percent", sa.Integer),
    )
    # Fixed singleton id (all-zero uuid) — MUST match SETTINGS_SINGLETON_ID in
    # app/services/pricing.py so get_or_create_settings is race-safe on this PK.
    op.bulk_insert(settings, [{
        "id": uuid.UUID(int=0), "referrer_reward_months": 1,
        "invitee_discount_percent": 15, "annual_discount_percent": 16,
    }])


def downgrade() -> None:
    op.drop_table("platform_settings")
    op.drop_table("pricing_plans")
