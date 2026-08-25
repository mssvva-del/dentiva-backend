"""Does the Stripe catalog we are about to charge against match what we sell?

Three ways billing breaks in production that no unit test can see, because all
three live in configuration rather than code:

  1. **Mode mismatch.** A live secret key with test price IDs still pasted, or
     the reverse. Stripe rejects the checkout, so the first person to find out
     is a clinic clicking Subscribe.
  2. **Stale amounts.** plans.py changes, nobody re-runs the catalog sync, and
     Stripe keeps charging last quarter's price. Nothing in the product looks
     wrong: the pricing page reads from plans.py, the invoice comes from Stripe,
     and only the customer sees both.
  3. **Missing price.** An id that was deleted, or belongs to another Stripe
     account entirely.

All three are silent until money is involved, which is the worst moment to
discover them. This asks Stripe what it actually holds and compares it with what
we actually sell.

Read-only: it fetches prices and never creates, updates or charges anything.
"""

from __future__ import annotations

import logging

from app.billing.plans import PLANS
from app.config import get_settings
from app.services.stripe_client import StripeError, _price_id, _stripe_request

logger = logging.getLogger(__name__)


def _expected_cents(plan, cycle: str) -> int:
    return plan.monthly_price_cents if cycle == "monthly" else plan.annual_total_cents


async def verify_catalog(*, transport=None) -> list[str]:
    """Compare every configured price against Stripe. Returns problems found.

    An empty list means the catalog is safe to charge against. Each string is
    written to be actionable on its own, because it is read in an alert at an
    hour when nobody wants to reconstruct the context.
    """
    settings = get_settings()
    key = settings.stripe_secret_key
    if not key:
        return []  # Billing not configured at all — a different, visible state.

    expect_live = key.startswith("sk_live_")
    problems: list[str] = []

    for plan in PLANS.values():
        for cycle in ("monthly", "annual"):
            price_id = _price_id(plan.key, cycle)
            if not price_id:
                problems.append(f"{plan.key}/{cycle}: no Stripe price configured")
                continue
            try:
                price = await _stripe_request(
                    "GET", f"/prices/{price_id}", transport=transport
                )
            except StripeError as exc:
                # 404 here usually means the id is from the OTHER mode, or from a
                # different Stripe account. Say both, since the fix differs.
                problems.append(
                    f"{plan.key}/{cycle}: Stripe rejected price {price_id} "
                    f"({exc}). Wrong mode, or wrong account."
                )
                continue

            if bool(price.get("livemode")) is not expect_live:
                problems.append(
                    f"{plan.key}/{cycle}: key is "
                    f"{'LIVE' if expect_live else 'TEST'} but price {price_id} is "
                    f"{'live' if price.get('livemode') else 'test'} — "
                    f"re-run scripts/sync_stripe_catalog.py in the right mode"
                )
            expected = _expected_cents(plan, cycle)
            actual = price.get("unit_amount")
            if actual is not None and int(actual) != expected:
                problems.append(
                    f"{plan.key}/{cycle}: we sell {expected}¢ but Stripe charges "
                    f"{actual}¢ — catalog is stale, re-run the sync"
                )

    return problems
