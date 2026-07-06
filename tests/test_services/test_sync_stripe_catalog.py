"""ADM7 — sync_stripe_catalog: idempotent reuse + fail-loud on immutable-price drift."""

from __future__ import annotations

import importlib

import httpx
import pytest


def _load(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mod = importlib.import_module("scripts.sync_stripe_catalog")
    return importlib.reload(mod)


def test_ensure_price_reuses_matching_amount(monkeypatch):
    mod = _load(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        # lookup returns an existing price with the SAME amount.
        return httpx.Response(200, json={"data": [{"id": "price_old", "unit_amount": 23900}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        pid = mod._ensure_price(c, product_id="prod_1", lookup_key="dentovox_after_hours_monthly",
                                amount_cents=23900, interval="month", dry=False)
    assert pid == "price_old"  # reused, no new price created


def test_ensure_price_fails_loud_on_amount_drift(monkeypatch):
    mod = _load(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        # existing price has a DIFFERENT amount → must refuse, never silently reuse.
        return httpx.Response(200, json={"data": [{"id": "price_old", "unit_amount": 14900}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(SystemExit, match="immutable"):
            mod._ensure_price(c, product_id="prod_1", lookup_key="dentovox_after_hours_monthly",
                              amount_cents=23900, interval="month", dry=False)
