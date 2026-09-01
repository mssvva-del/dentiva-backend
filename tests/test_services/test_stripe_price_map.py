"""ADM7 — Stripe price-id mapping for the new grid + legacy-key fallback."""

from __future__ import annotations

import app.services.stripe_client as sc


class _Cfg:
    stripe_price_overflow_monthly = "price_ov_m"
    stripe_price_overflow_annual = "price_ov_y"
    stripe_price_front_desk_monthly = "price_fd_m"
    stripe_price_front_desk_annual = "price_fd_y"
    stripe_price_revenue_monthly = "price_rv_m"
    stripe_price_revenue_annual = "price_rv_y"
    stripe_price_multi_monthly = "price_mu_m"
    stripe_price_multi_annual = "price_mu_y"


def test_price_id_new_keys(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    assert sc._price_id("overflow", "monthly") == "price_ov_m"
    assert sc._price_id("front_desk", "annual") == "price_fd_y"
    assert sc._price_id("revenue", "monthly") == "price_rv_m"
    assert sc._price_id("multi", "annual") == "price_mu_y"


def test_price_id_legacy_keys_resolve(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    # Old checkout links must still resolve to the current tier's price.
    assert sc._price_id("starter", "monthly") == "price_ov_m"
    assert sc._price_id("practice", "annual") == "price_fd_y"
    assert sc._price_id("group", "monthly") == "price_mu_m"


def test_price_id_unknown_is_empty(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    assert sc._price_id("nope", "monthly") == ""
    assert sc._price_id("overflow", "weekly") == ""
