"""Create/verify the Stripe Product+Price catalog for the ADM7 grid.

Idempotent: prices are keyed by a stable Stripe `lookup_key`
(dentovox_<plan>_<cycle>), so re-running finds the existing price instead of
making a duplicate. Prints the STRIPE_PRICE_* env assignments to paste into
Railway (test first, then live after "Switch to live account").

Usage:
    STRIPE_SECRET_KEY=sk_test_... python3 scripts/sync_stripe_catalog.py
    STRIPE_SECRET_KEY=sk_test_... python3 scripts/sync_stripe_catalog.py --dry-run

Money comes from app.billing.plans (single source of truth). Annual price is the
discounted YEARLY total billed once/year (interval=year), matching how
Subscription.mrr uses annual_monthly_equivalent_cents for display.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.billing.plans import PLANS  # noqa: E402

_API = "https://api.stripe.com/v1"


def _key() -> str:
    k = os.environ.get("STRIPE_SECRET_KEY", "")
    if not k:
        raise SystemExit("STRIPE_SECRET_KEY not set.")
    return k


def _req(client: httpx.Client, method: str, path: str, data: dict | None = None) -> dict:
    resp = client.request(method, f"{_API}{path}", data=data, auth=(_key(), ""))
    if resp.status_code >= 400:
        raise SystemExit(f"Stripe {resp.status_code} on {method} {path}: {resp.text[:300]}")
    return resp.json()


def _find_price(client: httpx.Client, lookup_key: str) -> dict | None:
    body = _req(client, "GET", f"/prices?lookup_keys[]={lookup_key}&limit=1")
    data = body.get("data", [])
    return data[0] if data else None


def _ensure_product(client: httpx.Client, plan_key: str, name: str, dry: bool) -> str:
    # Find an existing product tagged with our plan_key (metadata), else create.
    found = _req(client, "GET", "/products?limit=100&active=true").get("data", [])
    for p in found:
        if (p.get("metadata") or {}).get("dentovox_plan") == plan_key:
            return p["id"]
    if dry:
        return f"<would-create product {plan_key}>"
    created = _req(client, "POST", "/products", {
        "name": f"Dentovox {name}",
        "metadata[dentovox_plan]": plan_key,
    })
    return created["id"]


def _ensure_price(
    client: httpx.Client, *, product_id: str, lookup_key: str,
    amount_cents: int, interval: str, dry: bool,
) -> str:
    existing = _find_price(client, lookup_key)
    if existing:
        # Stripe unit_amount is IMMUTABLE. If the catalog changed the price, we
        # must NOT silently reuse the old one (that would bill the wrong amount).
        # Fail loudly so the operator migrates deliberately: bump the lookup_key
        # (e.g. dentovox_<plan>_<cycle>_v2), create the new price, then move
        # subscriptions over — a decision, not a silent default.
        if int(existing["unit_amount"]) != amount_cents:
            raise SystemExit(
                f"REFUSING to reuse {lookup_key}: Stripe price is "
                f"{existing['unit_amount']}¢ but the catalog wants {amount_cents}¢. "
                f"Stripe prices are immutable — bump the lookup_key to create a new "
                f"price and migrate subscriptions."
            )
        return existing["id"]
    if dry:
        return f"<would-create price {lookup_key} {amount_cents}¢/{interval}>"
    created = _req(client, "POST", "/prices", {
        "product": product_id,
        "currency": "usd",
        "unit_amount": str(amount_cents),
        "recurring[interval]": interval,
        "lookup_key": lookup_key,
        "transfer_lookup_key": "true",
    })
    return created["id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env_lines: list[str] = []
    with httpx.Client(timeout=30) as client:
        for plan_key, plan in PLANS.items():
            product_id = _ensure_product(client, plan_key, plan.name, args.dry_run)
            print(f"{plan.name} ({plan_key}) → product {product_id}")
            for cycle, amount, interval in (
                ("monthly", plan.monthly_price_cents, "month"),
                ("annual", plan.annual_total_cents, "year"),
            ):
                lk = f"dentovox_{plan_key}_{cycle}"
                price_id = _ensure_price(
                    client, product_id=product_id, lookup_key=lk,
                    amount_cents=amount, interval=interval, dry=args.dry_run,
                )
                env = f"STRIPE_PRICE_{plan_key.upper()}_{cycle.upper()}"
                env_lines.append(f"{env}={price_id}")
                print(f"  {cycle:8} {amount:>7}¢  {lk} → {price_id}")

    print("\n# Paste into Railway → dentiva-backend → Variables:")
    print("\n".join(env_lines))


if __name__ == "__main__":
    main()
