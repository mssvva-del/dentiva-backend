"""Dentiva plan catalog (Platform Iter 1, Phase D).

SINGLE SOURCE OF TRUTH for pricing. Sergio's FINAL numbers (2026-06):
  Starter  $149/mo · 500 min  · overage $0.30/min
  Practice $349/mo · 1000 min · overage $0.25/min
  Group    $599/mo · 2000 min · overage $0.20/min  (per location)
  Annual billing: 17% discount on the yearly total.

All money is stored and computed in INTEGER CENTS — never floats — so rounding is
exact and matches Stripe. The admin panel (Phase E) may override price / included
minutes / overage per client via the subscription row; this catalog is the
DEFAULT a plan starts from.

These are plan *definitions*, not Stripe IDs. The real Stripe Price IDs are
injected via env at Phase D wiring (test mode first); see app/config Stripe block.
"""

from __future__ import annotations

from dataclasses import dataclass

# Annual discount: 17% off the 12-month total (≈2 months free).
ANNUAL_DISCOUNT = 0.17


@dataclass(frozen=True)
class Plan:
    key: str                 # 'starter' | 'practice' | 'group'
    name: str
    monthly_price_cents: int
    included_minutes: int
    overage_cents_per_min: int   # charged per minute beyond included_minutes
    per_location: bool = False   # Group is priced per location

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
    "starter": Plan(
        key="starter", name="Starter",
        monthly_price_cents=14900, included_minutes=500, overage_cents_per_min=30,
    ),
    "practice": Plan(
        key="practice", name="Practice",
        monthly_price_cents=34900, included_minutes=1000, overage_cents_per_min=25,
    ),
    "group": Plan(
        key="group", name="Group",
        monthly_price_cents=59900, included_minutes=2000, overage_cents_per_min=20,
        per_location=True,
    ),
}

# Subscription statuses we model. 'pilot' = concierge clinic on a manual deal
# (free or setup-fee only, set by super_admin) — billing runs but isn't enforced.
SUBSCRIPTION_STATUSES = frozenset({
    "trialing", "active", "pilot", "past_due", "suspended", "cancelled",
})

# Practice lifecycle statuses that mean "service is usable" (not suspended).
ACTIVE_PRACTICE_STATUSES = frozenset({"trial", "pilot", "active"})


def get_plan(key: str) -> Plan | None:
    return PLANS.get(key)


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
