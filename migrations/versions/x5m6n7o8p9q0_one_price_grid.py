"""one price grid: the marketing table says what billing charges

Three sources named three different grids. The site (static HTML) and the
marketing table read $249/$399/$599/$899 with names from July; the billing
catalog and Stripe — derived from a margin target on 2026-09-01 — charge
$299/$499/$749/$649 under other names, with 400/650/1000/900 minutes and one
39c overage. A clinic read "Core, $399" on the site and was offered "Front Desk,
$499, 650 minutes" at checkout.

The marketing rows are rewritten to the catalog: same keys, same prices, same
allowances, same overage, annual discount 10%. A test now holds the two
together, so the next divergence fails a build instead of a sale.

Revision ID: x5m6n7o8p9q0
Revises: w4l5m6n7o8p9
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "x5m6n7o8p9q0"
down_revision = "w4l5m6n7o8p9"
branch_labels = None
depends_on = None

# key, name, monthly¢, annual/mo¢ (−10%), minutes, overage¢, reactivation, per_loc, highlight, sort
_GRID = (
    ("overflow",   "Overflow",       29900, 26910,  400, 39, "basic", False, False, 0),
    ("front_desk", "Front Desk",     49900, 44910,  650, 39, "full",  False, True,  1),
    ("revenue",    "Revenue",        74900, 67410, 1000, 39, "full",  False, False, 2),
    ("multi",      "Multi-Location", 64900, 58410,  900, 39, "full",  True,  False, 3),
)
_OLD_KEYS = ("after_hours", "full_time", "growth", "multi")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM pricing_plans WHERE plan_key = ANY(:keys)"),
                 {"keys": list(_OLD_KEYS)})
    for key, name, monthly, annual, minutes, overage, react, per_loc, hl, sort in _GRID:
        conn.execute(sa.text(
            "INSERT INTO pricing_plans (id, plan_key, name, monthly_cents, annual_monthly_cents, "
            "soft_cap_minutes, overage_cents_per_min, reactivation_level, per_location, highlight, "
            "sort_order, is_active, created_at, updated_at) VALUES (:id, :key, :name, :monthly, "
            ":annual, :minutes, :overage, :react, :per_loc, :hl, :sort, true, now(), now())"
        ), {"id": str(uuid.uuid4()), "key": key, "name": name, "monthly": monthly,
            "annual": annual, "minutes": minutes, "overage": overage, "react": react,
            "per_loc": per_loc, "hl": hl, "sort": sort})
    conn.execute(sa.text("UPDATE platform_settings SET annual_discount_percent = 10"))


def downgrade() -> None:
    # The July grid, as h9c0d1e2f3a4 left it.
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM pricing_plans WHERE plan_key = ANY(:keys)"),
                 {"keys": [g[0] for g in _GRID]})
    for key, name, monthly, annual, minutes, overage, react, per_loc, hl, sort in (
        ("after_hours", "After-Hours",    24900, 21165, 1500, 18, "basic", False, False, 0),
        ("full_time",   "Full-Time",      39900, 33915, 2500, 15, "full",  False, True,  1),
        ("growth",      "Growth",         59900, 50915, 4000, 13, "full",  False, False, 2),
        ("multi",       "Multi-Location", 89900, 76415, 3000, 11, "full",  True,  False, 3),
    ):
        conn.execute(sa.text(
            "INSERT INTO pricing_plans (id, plan_key, name, monthly_cents, annual_monthly_cents, "
            "soft_cap_minutes, overage_cents_per_min, reactivation_level, per_location, highlight, "
            "sort_order, is_active, created_at, updated_at) VALUES (:id, :key, :name, :monthly, "
            ":annual, :minutes, :overage, :react, :per_loc, :hl, :sort, true, now(), now())"
        ), {"id": str(uuid.uuid4()), "key": key, "name": name, "monthly": monthly,
            "annual": annual, "minutes": minutes, "overage": overage, "react": react,
            "per_loc": per_loc, "hl": hl, "sort": sort})
    conn.execute(sa.text("UPDATE platform_settings SET annual_discount_percent = 15"))
