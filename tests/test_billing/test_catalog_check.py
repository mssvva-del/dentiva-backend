"""The catalog check exists for bugs that live in configuration, not code.

Every case here is something that passes every other test in this suite and then
charges a real clinic the wrong amount, or fails at the checkout with a card
already in hand.
"""

import httpx
import pytest

from app.billing.catalog_check import verify_catalog
from app.billing.plans import PLANS
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _prices(monkeypatch, *, key="sk_test_x"):
    """Configure every plan/cycle with a predictable price id."""
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", key)
    for plan in PLANS.values():
        for cycle in ("monthly", "annual"):
            monkeypatch.setattr(
                settings, f"stripe_price_{plan.key}_{cycle}", f"price_{plan.key}_{cycle}"
            )


def _stripe(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _price_body(plan, cycle, *, livemode=False, cents=None):
    return {
        "id": f"price_{plan.key}_{cycle}",
        "livemode": livemode,
        "unit_amount": cents if cents is not None else (
            plan.monthly_price_cents if cycle == "monthly" else plan.annual_total_cents
        ),
    }


def _lookup(path: str):
    """price_<plan>_<cycle> → (plan, cycle)."""
    ident = path.rsplit("/", 1)[-1]
    for plan in PLANS.values():
        for cycle in ("monthly", "annual"):
            if ident == f"price_{plan.key}_{cycle}":
                return plan, cycle
    raise AssertionError(f"unexpected price fetch: {path}")


async def test_a_matching_catalog_reports_nothing(monkeypatch):
    _prices(monkeypatch)

    def handler(request):
        plan, cycle = _lookup(request.url.path)
        return httpx.Response(200, json=_price_body(plan, cycle, livemode=False))

    assert await verify_catalog(transport=_stripe(handler)) == []


async def test_live_key_with_test_prices_is_caught(monkeypatch):
    """The launch-day bug: the live key goes into Railway, the test price ids are
    still sitting next to it, and the first clinic to click Subscribe finds out."""
    _prices(monkeypatch, key="sk_live_x")

    def handler(request):
        plan, cycle = _lookup(request.url.path)
        # Stripe answers, but these are test-mode objects.
        return httpx.Response(200, json=_price_body(plan, cycle, livemode=False))

    problems = await verify_catalog(transport=_stripe(handler))
    assert len(problems) == len(PLANS) * 2
    assert all("LIVE" in p and "test" in p for p in problems)
    assert all("sync_stripe_catalog" in p for p in problems)


async def test_a_stale_amount_is_caught(monkeypatch):
    """plans.py changed and nobody re-ran the sync. Nothing in the product looks
    wrong — the pricing page reads our number, the invoice carries Stripe's, and
    only the customer ever sees both."""
    _prices(monkeypatch)
    target = PLANS["after_hours"]

    def handler(request):
        plan, cycle = _lookup(request.url.path)
        stale = plan is target and cycle == "monthly"
        return httpx.Response(200, json=_price_body(
            plan, cycle, livemode=False,
            cents=plan.monthly_price_cents + 5000 if stale else None,
        ))

    problems = await verify_catalog(transport=_stripe(handler))
    assert len(problems) == 1
    assert f"{target.key}/monthly" in problems[0]
    assert str(target.monthly_price_cents) in problems[0]


async def test_a_price_stripe_does_not_have_is_caught(monkeypatch):
    """A deleted price, or an id from a different Stripe account."""
    _prices(monkeypatch)

    def handler(request):
        return httpx.Response(404, json={"error": {"message": "No such price"}})

    problems = await verify_catalog(transport=_stripe(handler))
    assert len(problems) == len(PLANS) * 2
    assert all("Wrong mode, or wrong account." in p for p in problems)


async def test_an_unconfigured_price_is_named(monkeypatch):
    _prices(monkeypatch)
    monkeypatch.setattr(get_settings(), "stripe_price_growth_annual", "")

    def handler(request):
        plan, cycle = _lookup(request.url.path)
        return httpx.Response(200, json=_price_body(plan, cycle, livemode=False))

    problems = await verify_catalog(transport=_stripe(handler))
    assert problems == ["growth/annual: no Stripe price configured"]


async def test_billing_switched_off_is_not_a_problem(monkeypatch):
    """No key at all is a visible, deliberate state — not a misconfiguration to
    page somebody about at 3am."""
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "")

    def handler(request):
        raise AssertionError("must not call Stripe without a key")

    assert await verify_catalog(transport=_stripe(handler)) == []
