"""ADM2 — coupons: Stripe client (mocked HTTP) + admin endpoints (gated, audited)."""

from __future__ import annotations

import uuid

import httpx
import pytest

import app.routes.admin as admin_mod
import app.services.stripe_client as sc
from app.models.dentiva_staff import DentivaStaff
from app.models.subscription import Subscription
from app.models.user import User
from tests.conftest import seed_practice


# ── stripe client (mocked transport) ─────────────────────────────────────────
class _Cfg:
    stripe_secret_key = "sk_test_x"


@pytest.fixture
def _stripe_cfg(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())


async def test_create_coupon_sends_form_fields(_stripe_cfg):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "co_1", "name": "Pilot20",
                                         "percent_off": 20.0, "duration": "repeating",
                                         "duration_in_months": 3, "valid": True})

    c = await sc.create_coupon(name="Pilot20", percent_off=20.0, duration="repeating",
                               duration_in_months=3,
                               transport=httpx.MockTransport(handler))
    assert c["id"] == "co_1"
    assert seen["path"] == "/v1/coupons"
    assert "percent_off=20.0" in seen["body"] and "duration_in_months=3" in seen["body"]


async def test_apply_coupon_hits_subscription(_stripe_cfg):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "sub_9", "discounts": ["co_1"]})

    await sc.apply_coupon_to_subscription("sub_9", "co_1",
                                          transport=httpx.MockTransport(handler))
    assert seen["path"] == "/v1/subscriptions/sub_9"
    assert "coupon%5D=co_1" in seen["body"] or "coupon]=co_1" in seen["body"]


async def test_stripe_4xx_raises_stripe_error(_stripe_cfg):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "No such coupon"}})

    with pytest.raises(sc.StripeError, match="No such coupon"):
        await sc.delete_coupon("co_missing", transport=httpx.MockTransport(handler))


async def test_no_key_raises_not_configured(monkeypatch):
    class _NoKey:
        stripe_secret_key = ""
    monkeypatch.setattr(sc, "get_settings", lambda: _NoKey())
    with pytest.raises(sc.BillingNotConfigured):
        await sc.list_coupons()


# ── admin endpoints ──────────────────────────────────────────────────────────
async def _internal(db_session, *, clerk_id, role):
    u = User(id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
             email=f"{clerk_id}@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()
    return u


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": "org_internal"}


async def test_admin_create_and_apply_coupon(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_c1", role="finance")  # MANAGE_SUBSCRIPTIONS
    practice, _ = await seed_practice(db_session, name="CpnCo",
                                      clerk_org_id="o_cpn", clerk_user_id="u_cpn")
    db_session.add(Subscription(
        id=uuid.uuid4(), practice_id=practice.id, plan="starter",
        billing_cycle="monthly", status="active", included_minutes=750,
        mrr_cents=24900, stripe_subscription_id="sub_live_1",
    ))
    await db_session.commit()

    async def _fake_create(**kw):
        return {"id": "co_new", "name": kw["name"], "percent_off": kw["percent_off"],
                "amount_off": None, "duration": kw["duration"],
                "duration_in_months": None, "valid": True}
    applied = {}

    async def _fake_apply(sub_id, coupon_id, **_kw):
        applied["sub"] = sub_id
        applied["coupon"] = coupon_id
        return {"id": sub_id}
    monkeypatch.setattr(admin_mod, "create_coupon", _fake_create)
    monkeypatch.setattr(admin_mod, "apply_coupon_to_subscription", _fake_apply)

    r = await client.post("/api/admin/coupons", headers=_h("fin_c1"),
                          json={"name": "Founding25", "percent_off": 25, "duration": "once"})
    assert r.status_code == 200 and r.json()["id"] == "co_new"

    r2 = await client.post(f"/api/admin/clinics/{practice.id}/apply-coupon",
                           headers=_h("fin_c1"), json={"coupon_id": "co_new"})
    assert r2.status_code == 200
    assert applied == {"sub": "sub_live_1", "coupon": "co_new"}


async def test_apply_coupon_no_stripe_sub_409(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_c2", role="finance")
    practice, _ = await seed_practice(db_session, name="NoStripe",
                                      clerk_org_id="o_ns", clerk_user_id="u_ns")
    db_session.add(Subscription(
        id=uuid.uuid4(), practice_id=practice.id, plan="starter",
        billing_cycle="monthly", status="trialing", included_minutes=750,
        mrr_cents=0, stripe_subscription_id=None,  # pilot without Stripe
    ))
    await db_session.commit()
    r = await client.post(f"/api/admin/clinics/{practice.id}/apply-coupon",
                          headers=_h("fin_c2"), json={"coupon_id": "co_x"})
    assert r.status_code == 409  # clear guidance, not a Stripe error


async def test_coupon_validation_422(client, db_session):
    await _internal(db_session, clerk_id="fin_c3", role="finance")
    # both kinds → 422; neither → 422; bad duration → 422
    for payload in (
        {"name": "X", "percent_off": 10, "amount_off_cents": 500},
        {"name": "X"},
        {"name": "X", "percent_off": 10, "duration": "weekly"},
        {"name": "X", "percent_off": 10, "duration": "repeating"},  # no months
    ):
        r = await client.post("/api/admin/coupons", headers=_h("fin_c3"), json=payload)
        assert r.status_code == 422, payload


async def test_coupons_denied_to_sales_and_clinic(client, db_session):
    await _internal(db_session, clerk_id="sales_c", role="sales")  # no MANAGE_SUBSCRIPTIONS
    assert (await client.get("/api/admin/coupons", headers=_h("sales_c"))).status_code == 403
    assert (await client.get("/api/admin/coupons",
                             headers=_h("random_clinic"))).status_code in (401, 403)
