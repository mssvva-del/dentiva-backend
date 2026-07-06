"""ADM7 — Stripe price-id mapping for the new grid + legacy-key fallback."""

from __future__ import annotations

import app.services.stripe_client as sc


class _Cfg:
    stripe_price_after_hours_monthly = "price_ah_m"
    stripe_price_after_hours_annual = "price_ah_y"
    stripe_price_full_time_monthly = "price_ft_m"
    stripe_price_full_time_annual = "price_ft_y"
    stripe_price_growth_monthly = "price_gr_m"
    stripe_price_growth_annual = "price_gr_y"
    stripe_price_multi_monthly = "price_mu_m"
    stripe_price_multi_annual = "price_mu_y"


def test_price_id_new_keys(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    assert sc._price_id("after_hours", "monthly") == "price_ah_m"
    assert sc._price_id("full_time", "annual") == "price_ft_y"
    assert sc._price_id("growth", "monthly") == "price_gr_m"
    assert sc._price_id("multi", "annual") == "price_mu_y"


def test_price_id_legacy_keys_resolve(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    # Old checkout links must still resolve to the current tier's price.
    assert sc._price_id("starter", "monthly") == "price_ah_m"
    assert sc._price_id("practice", "annual") == "price_ft_y"
    assert sc._price_id("group", "monthly") == "price_mu_m"


def test_price_id_unknown_is_empty(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    assert sc._price_id("nope", "monthly") == ""
    assert sc._price_id("after_hours", "weekly") == ""
