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

# Annual discount, cut from 15% to 10%.
#
# A discount is a margin decision, not a SaaS habit. Against the 65% the grid
# targets, 15% off leaves roughly 59% and buys cash flow with the one thing we
# were short of. 10% leaves about 61% and still gives a practice a real reason
# to commit for a year. Revisit upward when the cost per minute is measured
# rather than estimated.
ANNUAL_DISCOUNT = 0.10

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
# Measured 2026-08: voice+telephony 14.8¢/min on Cartesia (was 17.3¢ on
# ElevenLabs). Held at the conservative integer — this drives the admin margin
# figures, and rounding our costs DOWN there would flatter every number a
# pricing decision leans on. NOT in this figure: NexHealth per-request fees
# (~$0.10/call × 4–6 calls per booked patient once the one-time free credit is
# spent, ≈10¢/min on a five-minute call). Estimate, not a measure — revisit
# when a per-location NexHealth deal replaces per-request billing.
ESTIMATED_COST_CENTS_PER_MIN = 15

# The number the GRID is derived from, which is not the same number.
#
# 15¢ is what we have MEASURED and is what the admin margin screens show. This
# one adds the PMS middleware we have not measured: NexHealth bills per request,
# roughly $0.10 each, at 4–6 requests to book one patient — about 10¢ over a
# five-minute call. Estimate, explicitly.
#
# Pricing is built on the pessimistic figure on purpose. If the estimate is too
# high we are leaving margin on the table, which is recoverable by raising
# allowances later. If it is too low and we priced on 15¢, every busy clinic
# loses us money and we find out from a quarter's accounts.
#
# It comes down two ways. One is a per-location NexHealth deal (~$75/location
# discussed, unsigned). The other is now CONFIRMED IN WRITING by Open Dental's
# VP of Development, 2026-08-07:
#
#   * read-only GET access: free
#   * $30 per location per month for our exact use — creating and updating
#     patients and appointments, excluding payments
#   * no flat fees, no monthly minimums, volume discounts available
#
# Flat is the word that matters. NexHealth charges per REQUEST, so its cost
# climbs with every call; $30 a month dilutes toward zero per minute as a clinic
# talks more. On this grid an Open Dental clinic runs 4-9 points better gross
# margin than a NexHealth one, and the gap widens with usage rather than
# narrowing — the opposite of the shape we have today.
#
# 25c is kept as the pricing basis because it is the PESSIMISTIC case, and the
# first clinic is on NexHealth. Open Dental practices simply come in above
# target, which is the right direction for an estimate to be wrong in.
PRICING_COST_CENTS_PER_MIN = 25

# Gross margin the grid must hold at FULL utilisation — the worst case, where a
# clinic uses every included minute. Anything less demanding prices for the
# customer who barely calls and punishes us for the one who loves the product.
TARGET_GROSS_MARGIN = 0.65


def included_minutes_for(monthly_price_cents: int, *,
                         target_margin: float = TARGET_GROSS_MARGIN,
                         cost_cents_per_min: int = PRICING_COST_CENTS_PER_MIN) -> int:
    """How many minutes a price can carry and still hold the target margin.

        minutes = price x (1 - margin) / cost_per_minute

    The previous grid had no such derivation. Its allowances implied revenue of
    15.0–16.6c per included minute against a 14.8c measured floor — the
    allowances had been chosen by feel, and the arithmetic simply had not been
    done. Comparable metered products sell 44–53c of revenue per included
    minute, which is what a grid looks like when it starts from a margin target.
    """
    return int(monthly_price_cents * (1 - target_margin) / cost_cents_per_min)


@dataclass(frozen=True)
class Plan:
    key: str                 # overflow | front_desk | revenue | multi
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


# One overage rate for every tier.
#
# The old grid charged LESS per extra minute the higher the plan — 18c, 15c,
# 13c, 11c — so a clinic growing into a bigger plan cost us more the more it
# used us, and three of the four rates sat below the 14.8c measured voice floor
# before any PMS cost at all. That is not a discount, it is a subsidy that grows
# with the customer's success.
#
# 39c holds roughly 36% margin on the 25c pricing cost, and about 62% if the
# cost lands nearer the measured 15c. It is above the 29c a metered competitor
# charges, which is the honest trade: they can go lower because their allowances
# are a third the size. The route to matching them runs through cost — at a 17c
# marginal cost, 29c carries the same 40% margin. Lower this when that happens,
# not before.
OVERAGE_CENTS_PER_MIN = 39

# Catalog keyed by plan id. Order matters for display (cheapest first).
#
# Tiers differ by WHAT THE PRODUCT DOES, not by which taps cost extra. Every one
# includes HIPAA and the BAA, English and Spanish, transcripts, analytics and
# PMS booking — a competitor markets against "surprise overages" and paid
# add-ons, and they are right to.
#
# Allowances come from included_minutes_for(), rounded DOWN to a round number.
# Nothing here is a judgement call about how many minutes feel generous.
PLANS: dict[str, Plan] = {
    "overflow": Plan(
        key="overflow", name="Overflow",
        # Busy, no answer, and the days the practice is closed. The entry
        # product: it takes nothing away from the front desk.
        monthly_price_cents=29900, included_minutes=400,
        overage_cents_per_min=OVERAGE_CENTS_PER_MIN,
    ),
    "front_desk": Plan(
        key="front_desk", name="Front Desk",
        # All-day coverage — the agent is the line, not the safety net.
        # 650, not the 700 this was first written as: the ceiling at $499 is 698
        # and 700 was a round number chosen by hand — the exact habit this grid
        # exists to end. The test caught it before the commit did.
        monthly_price_cents=49900, included_minutes=650,
        overage_cents_per_min=OVERAGE_CENTS_PER_MIN,
    ),
    "revenue": Plan(
        key="revenue", name="Revenue",
        # Front Desk plus the outbound work: reactivation, recalls, and calling
        # back the people who rang this week and did not book. The only tier
        # that MAKES a clinic money rather than saving it — and the only one
        # gated on outbound actually being switched on (see routes/billing.py).
        monthly_price_cents=74900, included_minutes=1000,
        overage_cents_per_min=OVERAGE_CENTS_PER_MIN,
    ),
    "multi": Plan(
        key="multi", name="Multi-Location",
        # Per location, minutes pooled across the group. Priced below Front Desk
        # per site because a group brings volume and one commercial
        # relationship, not because its minutes are cheaper to serve.
        monthly_price_cents=64900, included_minutes=900,
        overage_cents_per_min=OVERAGE_CENTS_PER_MIN,
        per_location=True,
    ),
}

# Deprecated pre-ADM7 keys (starter/practice/group) mapped to the nearest current
# tier. Keeps any existing subscription row / demo / old checkout link resolving
# instead of crashing on get_plan() — remove once no rows reference them.
_LEGACY_ALIASES: dict[str, str] = {
    "starter": "overflow",
    "practice": "front_desk",
    "group": "multi",
    # The 2026-08 grid, replaced when the allowances turned out never to have
    # been derived from a margin target. No practice was on a paid plan when it
    # changed — the aliases exist for old checkout links and demo rows, not for
    # a migration that had to happen.
    "after_hours": "overflow",
    "full_time": "front_desk",
    "growth": "revenue",
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
