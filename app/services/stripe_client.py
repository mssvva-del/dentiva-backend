"""Minimal Stripe REST client for Checkout (Platform Iter 1, Phase D).

Only what billing needs: create a Checkout Session for a plan. We call Stripe's
REST API directly with httpx (form-encoded) instead of the heavy `stripe` SDK.

GUARDED: when STRIPE_SECRET_KEY is empty (keys arrive later), every call raises
BillingNotConfigured, which the route turns into a clean 503 — never a crash.
The Price-ID mapping comes from env (test-mode IDs first).
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_STRIPE_API = "https://api.stripe.com/v1"


class BillingNotConfigured(Exception):
    """Raised when a Stripe call is attempted without configured keys."""


def _price_id(plan_key: str, billing_cycle: str) -> str:
    """Resolve our plan+cycle to the configured Stripe Price ID (or '')."""
    s = get_settings()
    table = {
        ("starter", "monthly"): s.stripe_price_starter_monthly,
        ("starter", "annual"): s.stripe_price_starter_annual,
        ("practice", "monthly"): s.stripe_price_practice_monthly,
        ("practice", "annual"): s.stripe_price_practice_annual,
        ("group", "monthly"): s.stripe_price_group_monthly,
        ("group", "annual"): s.stripe_price_group_annual,
    }
    return table.get((plan_key, billing_cycle), "")


async def create_checkout_session(
    *,
    practice_id: str,
    plan_key: str,
    billing_cycle: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> str:
    """Create a Stripe Checkout Session and return its hosted URL.

    Raises BillingNotConfigured if keys/price aren't set yet (Iter 1 default).
    practice_id is stamped into metadata so the webhook can reconcile on
    checkout.session.completed.
    """
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise BillingNotConfigured("STRIPE_SECRET_KEY not set")
    price = _price_id(plan_key, billing_cycle)
    if not price:
        raise BillingNotConfigured(f"no Stripe price for {plan_key}/{billing_cycle}")

    # Stripe wants form-encoded params with bracket notation for nested fields.
    data = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[practice_id]": practice_id,
        "metadata[plan]": plan_key,
        "metadata[billing_cycle]": billing_cycle,
        # Propagate to the subscription so subscription.* events carry it too.
        "subscription_data[metadata][practice_id]": practice_id,
    }
    if customer_email:
        data["customer_email"] = customer_email

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_STRIPE_API}/checkout/sessions",
            data=data,
            auth=(settings.stripe_secret_key, ""),
        )
    if resp.status_code >= 400:
        raise BillingNotConfigured(f"Stripe error {resp.status_code}: {resp.text[:200]}")
    return resp.json()["url"]


# ---------------------------------------------------------------------------
# Coupons (ADM2) — Stripe-native discounts, applied to a clinic's subscription.
# We deliberately use Stripe Coupon objects instead of a homegrown discount table:
# Stripe then handles proration/invoicing math and the discount shows up on real
# invoices. All calls form-encoded httpx (same pattern as checkout above), with an
# injectable transport so tests never hit the network.
# ---------------------------------------------------------------------------


class StripeError(Exception):
    """Stripe rejected the request (4xx) — surfaced to the admin as a 422/502."""


def _require_key() -> str:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise BillingNotConfigured("STRIPE_SECRET_KEY not set")
    return settings.stripe_secret_key


async def _stripe_request(
    method: str,
    path: str,
    data: dict | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    key = _require_key()
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        resp = await client.request(
            method, f"{_STRIPE_API}{path}", data=data, auth=(key, "")
        )
    if resp.status_code >= 400:
        # Stripe error bodies are JSON {error:{message}} — surface the message,
        # bounded, never the key.
        try:
            msg = resp.json().get("error", {}).get("message", "")[:200]
        except Exception:  # noqa: BLE001
            msg = resp.text[:200]
        raise StripeError(f"Stripe {resp.status_code}: {msg}")
    return resp.json()


async def create_coupon(
    *,
    name: str,
    percent_off: float | None = None,
    amount_off_cents: int | None = None,
    duration: str = "once",
    duration_in_months: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Create a Stripe coupon. Exactly one of percent_off / amount_off_cents.
    duration: once | repeating (needs duration_in_months) | forever."""
    data: dict = {"name": name, "duration": duration}
    if percent_off is not None:
        data["percent_off"] = str(percent_off)
    if amount_off_cents is not None:
        data["amount_off"] = str(amount_off_cents)
        data["currency"] = "usd"
    if duration == "repeating" and duration_in_months:
        data["duration_in_months"] = str(duration_in_months)
    return await _stripe_request("POST", "/coupons", data, transport=transport)


async def list_coupons(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict]:
    body = await _stripe_request("GET", "/coupons?limit=100", transport=transport)
    return body.get("data", [])


async def delete_coupon(
    coupon_id: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    await _stripe_request("DELETE", f"/coupons/{coupon_id}", transport=transport)


async def apply_coupon_to_subscription(
    stripe_subscription_id: str,
    coupon_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Attach a coupon to an existing Stripe subscription (discount applies from
    the next invoice per the coupon's duration)."""
    return await _stripe_request(
        "POST",
        f"/subscriptions/{stripe_subscription_id}",
        {"discounts[0][coupon]": coupon_id},
        transport=transport,
    )
