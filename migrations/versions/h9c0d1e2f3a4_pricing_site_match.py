"""Update pricing grid to match the public site: $249/399/599/899, annual -15%.

Sergio 2026-07-13: the marketing site is canonical for the displayed price. The
seeded grid (c4x5y6z7a8b9) used $239/379/579/849 @ -16%; bring the DB pricing_plans
rows + platform_settings.annual_discount_percent in line so /api/pricing, the admin
editor, and the site all agree. Safe: no clinics have edited these rows yet.

Revision ID: h9c0d1e2f3a4
Revises: g8b9c0d1e2f3
"""
from __future__ import annotations

from alembic import op

revision = "h9c0d1e2f3a4"
down_revision = "g8b9c0d1e2f3"
branch_labels = None
depends_on = None

# (plan_key, monthly_cents, annual_monthly_cents = round(monthly * 0.85))
_NEW = [
    ("after_hours", 24900, 21165),
    ("full_time", 39900, 33915),
    ("growth", 59900, 50915),
    ("multi", 89900, 76415),
]
_OLD = [
    ("after_hours", 23900, 20100),
    ("full_time", 37900, 31800),
    ("growth", 57900, 48600),
    ("multi", 84900, 71300),
]


def _apply(rows: list[tuple[str, int, int]], annual_pct: int) -> None:
    for key, monthly, annual_monthly in rows:
        op.execute(
            "UPDATE pricing_plans "
            f"SET monthly_cents = {monthly}, annual_monthly_cents = {annual_monthly} "
            f"WHERE plan_key = '{key}'"
        )
    op.execute(
        f"UPDATE platform_settings SET annual_discount_percent = {annual_pct}"
    )


def upgrade() -> None:
    _apply(_NEW, 15)


def downgrade() -> None:
    _apply(_OLD, 16)
