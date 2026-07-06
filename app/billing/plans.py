"""Dentiva plan catalog (billing/metering).

SINGLE SOURCE OF TRUTH for the BILLED catalog. Sergio's approved grid (ADM7,
2026-07-05 — see dentiva-docs PRICING_DECISION):
  After-Hours    $239/mo · 1500 min · overage $0.18/min
  Full-Time      $379/mo · 2500 min · overage $0.15/min   (most popular)
  Growth         $579/mo · 4000 min · overage $0.13/min
  Multi-Location $849/mo · 3000 min · overage $0.11/min   (per location)
  Annual billing: 16% discount on the yearly total.

This catalog drives metering (included minutes, overage) and the Stripe checkout
price mapping. It mirrors the ADM6 marketing grid (pricing_plans table) key-for-
key; the marketing table is what the site EDITS/displays, this is what BILLS —
keep the two in sync when tiers change.

All money is stored and computed in INTEGER CENTS — never floats. The admin panel
may override price / minutes / overage per client via the subscription row; this
catalog is the DEFAULT a plan starts from. Real Stripe Price IDs are injected via
env (scripts/sync_stripe_catalog.py creates them); see app/config Stripe block.
"""

from __future__ import annotations

from dataclasses import dataclass

# Annual discount: 16% off the 12-month total (matches the ADM6 grid / site).
ANNUAL_DISCOUNT = 0.16

# Rough per-minute cost of a call (Retell + LLM + Cartesia TTS), in cents. Used by
# the admin margin view only — a planning estimate, NOT an invoice input. Tune as
# real vendor costs land; keep it conservative (over-estimate) so margin isn't
# flattering.
ESTIMATED_COST_CENTS_PER_MIN = 8


@dataclass(frozen=True)
class Plan:
    key: str                 # after_hours | full_time | growth | multi
    name: str
    monthly_price_cents: int
    included_minutes: int         # fair-use soft cap
    overage_cents_per_min: int   # charged per minute beyond included_minutes
    per_location: bool = False   # Multi-Location is priced per location

    @property
    def annual_total_cents(self) -> int:
        """Yearly price with the annual discount, in cents (rounded to a cent)."""
        gross = self.monthly_price_cents * 12
        return round(gross * (1 - ANNUAL_DISCOUNT))

    @property
    def annual_monthly_equivalent_cents(self) -> int:
        """Annual total spread over 12 months — for 'as low as $X/mo' display."""
        return round(self.annual_total_cents / 12)


# Catalog keyed by plan id. Order matters for display (cheapest first).
PLANS: dict[str, Plan] = {
    "after_hours": Plan(
        key="after_hours", name="After-Hours",
        monthly_price_cents=23900, included_minutes=1500, overage_cents_per_min=18,
    ),
    "full_time": Plan(
        key="full_time", name="Full-Time",
        monthly_price_cents=37900, included_minutes=2500, overage_cents_per_min=15,
    ),
    "growth": Plan(
        key="growth", name="Growth",
        monthly_price_cents=57900, included_minutes=4000, overage_cents_per_min=13,
    ),
    "multi": Plan(
        key="multi", name="Multi-Location",
        monthly_price_cents=84900, included_minutes=3000, overage_cents_per_min=11,
        per_location=True,
    ),
}

# Deprecated pre-ADM7 keys (starter/practice/group) mapped to the nearest current
# tier. Keeps any existing subscription row / demo / old checkout link resolving
# instead of crashing on get_plan() — remove once no rows reference them.
_LEGACY_ALIASES: dict[str, str] = {
    "starter": "after_hours",
    "practice": "full_time",
    "group": "multi",
}

# Subscription statuses we model. 'pilot' = concierge clinic on a manual deal
# (free or setup-fee only, set by super_admin) — billing runs but isn't enforced.
SUBSCRIPTION_STATUSES = frozenset({
    "trialing", "active", "pilot", "past_due", "suspended", "cancelled",
})

# Practice lifecycle statuses that mean "service is usable" (not suspended).
ACTIVE_PRACTICE_STATUSES = frozenset({"trial", "pilot", "active"})


def get_plan(key: str) -> Plan | None:
    """Resolve a plan by key, transparently mapping deprecated legacy keys
    (starter/practice/group) to their current tier."""
    return PLANS.get(key) or PLANS.get(_LEGACY_ALIASES.get(key, ""))


def compute_overage_cents(
    minutes_used: float, included_minutes: int, overage_cents_per_min: int
) -> int:
    """Cents owed for minutes beyond the plan's included allowance.

    Partial minutes are billed (ceil) — matches how telephony overage normally
    works and avoids under-charging. Never negative.
    """
    import math

    over = max(0.0, minutes_used - included_minutes)
    return math.ceil(over) * overage_cents_per_min
