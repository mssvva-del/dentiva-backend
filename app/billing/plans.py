"""Dentiva plan catalog (billing/metering).

SINGLE SOURCE OF TRUTH for the BILLED catalog. Sergio's approved grid (updated
2026-07-13 to match the public site — site is canonical for the displayed price):
  After-Hours    $249/mo · 1500 min · overage $0.18/min

MARGIN CHECK against the measured 17.3¢/min floor (voice 15.8 + telephony 1.5):

  plan            revenue/min if the whole bucket is used   overage vs floor
  After-Hours     16.6¢   (break-even at 1439 of 1500 min)  +0.7¢
  Full-Time       16.0¢   (break-even at 2306 of 2500 min)  -2.3¢
  Growth          15.0¢   (break-even at 3462 of 4000 min)  -4.3¢
  Multi-Location  30.0¢   (break-even at 5197 of 3000 min)  -6.3¢

Included minutes are a fair-use cap, not expected usage, so a clinic that talks
for 500 minutes on Full-Time is very profitable — the included-minute column is
the worst case, not the forecast. The unambiguous problem is the overage column:
three of four plans charge LESS per extra minute than the minute costs, and the
rate falls as the tier rises, so a clinic growing into a bigger plan loses us
more money the more it uses us. Overage should clear the floor with margin at
every tier.
  Full-Time      $399/mo · 2500 min · overage $0.15/min   (most popular)
  Growth         $599/mo · 4000 min · overage $0.13/min
  Multi-Location $899/mo · 3000 min · overage $0.11/min   (per location)
  Annual billing: 15% discount on the yearly total.

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

# Annual discount: 15% off the 12-month total (matches the public site).
ANNUAL_DISCOUNT = 0.15

# Rough per-minute cost of a call (Retell + LLM + Cartesia TTS), in cents. Used by
# the admin margin view only — a planning estimate, NOT an invoice input. Tune as
# real vendor costs land; keep it conservative (over-estimate) so margin isn't
# flattering.
# MEASURED on a real production call (2026-07-31), not estimated: Retell voice
# engine 5.5 + ElevenLabs flash TTS 4.0 + gpt-4.1 4.5 + LLM token surcharge 0.9
# = 15.8¢/min, plus 1.5¢/min telephony on a phone call (0 on a browser call).
# Rounded to 17 because the margin view exists to stop us fooling ourselves.
#
# It read 8 until today — less than half the truth — so every margin figure an
# admin looked at was roughly double the real one, including while the plan grid
# below was being set.
ESTIMATED_COST_CENTS_PER_MIN = 17


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
        monthly_price_cents=24900, included_minutes=1500, overage_cents_per_min=18,
    ),
    "full_time": Plan(
        key="full_time", name="Full-Time",
        monthly_price_cents=39900, included_minutes=2500, overage_cents_per_min=15,
    ),
    "growth": Plan(
        key="growth", name="Growth",
        monthly_price_cents=59900, included_minutes=4000, overage_cents_per_min=13,
    ),
    "multi": Plan(
        key="multi", name="Multi-Location",
        monthly_price_cents=89900, included_minutes=3000, overage_cents_per_min=11,
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
