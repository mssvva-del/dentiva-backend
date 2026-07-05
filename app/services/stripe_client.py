"""Minimal Stripe REST client for Checkout (Platform Iter 1, Phase D).

Only what billing needs: create a Checkout Session for a plan. We call Stripe's
REST API directly with httpx (form-encoded) instead of the heavy `stripe` SDK.

GUARDED: when STRIPE_SECRET_KEY is empty (keys arrive later), every call raises
BillingNotConfigured, which the route turns into a clean 503 — never a crash.
The Price-ID mapping comes from env (test-mode IDs first).
"""

from __future__ import annotations

import re

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
    """Stripe request failed. ``status_code`` lets the route map 4xx → 422 and
    upstream 5xx / transport failures → 502 (honest gateway error)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_key() -> str:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise BillingNotConfigured("STRIPE_SECRET_KEY not set")
    return settings.stripe_secret_key


# Pinned API version: modern account defaults (2025-03-31.basil+) REMOVED
# invoice.payment_intent, which the refund flow reads — live-verified against our
# test account (default 2026-06-24.dahlia has no payment_intent at all). Pinning
# also makes behavior reproducible regardless of the account's default version.
_STRIPE_VERSION = "2024-06-20"


async def _stripe_request(
    method: str,
    path: str,
    data: dict | None = None,
    *,
    idempotency_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    key = _require_key()
    headers = {"Stripe-Version": _STRIPE_VERSION}
    if idempotency_key:
        # Stripe dedupes identical requests carrying the same key — the refund key
        # is derived from the invoice's prior state, so an accidental double-submit
        # of the SAME refund collapses into one money movement.
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            resp = await client.request(
                method, f"{_STRIPE_API}{path}", data=data, auth=(key, ""),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        # Stripe unreachable / timeout — never let a transport error escape as a
        # raw 500; the route maps this to a clean 502.
        raise StripeError(
            f"Stripe unreachable: {type(exc).__name__}", status_code=502
        ) from exc
    if resp.status_code >= 400:
        # Stripe error bodies are JSON {error:{message}} — surface the message,
        # bounded, never the key.
        try:
            msg = resp.json().get("error", {}).get("message", "")[:200]
        except Exception:  # noqa: BLE001
            msg = resp.text[:200]
        raise StripeError(f"Stripe {resp.status_code}: {msg}", status_code=resp.status_code)
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


def _safe_id(value: str, what: str) -> str:
    """Validate a Stripe object id before interpolating it into a URL path —
    defense-in-depth against path tricks if a future caller passes raw input."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value or ""):
        raise StripeError(f"invalid {what} id", status_code=422)
    return value


async def delete_coupon(
    coupon_id: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    await _stripe_request(
        "DELETE", f"/coupons/{_safe_id(coupon_id, 'coupon')}", transport=transport
    )


async def apply_coupon_to_subscription(
    stripe_subscription_id: str,
    coupon_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Attach a coupon to an existing Stripe subscription (discount applies from
    the next invoice per the coupon's duration).

    NOTE: ``discounts[0][coupon]`` REPLACES the subscription's discounts array —
    one discount per clinic by design; applying coupon B removes coupon A."""
    return await _stripe_request(
        "POST",
        f"/subscriptions/{_safe_id(stripe_subscription_id, 'subscription')}",
        {"discounts[0][coupon]": _safe_id(coupon_id, "coupon")},
        transport=transport,
    )


async def cancel_subscription(
    stripe_subscription_id: str,
    *,
    at_period_end: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Cancel a Stripe subscription.

    at_period_end=True (default): the clinic keeps service until the period it
    already paid for runs out, then Stripe stops billing — sends
    customer.subscription.updated now and ...deleted at period end.
    at_period_end=False: immediate hard cancel (DELETE) — Stripe stops billing and
    sends customer.subscription.deleted right away. No proration/refund happens
    automatically; use the refund endpoint for that.

    Deliberately NO Idempotency-Key (unlike refund_invoice): these are set-value
    operations — repeating them converges to the same state, double-submits are
    already serialized by the route's row lock + status guards, and a state-derived
    key would make Stripe REPLAY a stale response across a cancel→resume→cancel
    cycle instead of re-applying the flag (real divergence, worse than none).
    """
    sid = _safe_id(stripe_subscription_id, "subscription")
    if at_period_end:
        return await _stripe_request(
            "POST", f"/subscriptions/{sid}",
            {"cancel_at_period_end": "true"}, transport=transport,
        )
    return await _stripe_request("DELETE", f"/subscriptions/{sid}", transport=transport)


async def resume_subscription(
    stripe_subscription_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Undo a pending at-period-end cancellation (only works BEFORE the period
    ends — a fully deleted subscription cannot be resumed, Stripe returns 4xx)."""
    return await _stripe_request(
        "POST", f"/subscriptions/{_safe_id(stripe_subscription_id, 'subscription')}",
        {"cancel_at_period_end": "false"}, transport=transport,
    )


async def refund_invoice(
    stripe_invoice_id: str,
    amount_cents: int | None = None,
    *,
    idempotency_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Refund a PAID Stripe invoice (fully, or partially when amount_cents given).

    Two steps: fetch the invoice to get its payment_intent (present on the pinned
    API version — see _STRIPE_VERSION), then create the refund against that
    payment. Stripe rejects refunding an unpaid invoice / exceeding the charged
    amount — those surface as StripeError(4xx) → 422 to the admin. The
    idempotency_key makes an accidental duplicate submit a no-op on Stripe's side.
    """
    inv = await _stripe_request(
        "GET", f"/invoices/{_safe_id(stripe_invoice_id, 'invoice')}",
        transport=transport,
    )
    payment_intent = inv.get("payment_intent")
    if not payment_intent:
        raise StripeError("invoice has no payment to refund", status_code=422)
    data: dict = {"payment_intent": payment_intent}
    if amount_cents is not None:
        data["amount"] = str(amount_cents)
    return await _stripe_request(
        "POST", "/refunds", data, idempotency_key=idempotency_key, transport=transport
    )
