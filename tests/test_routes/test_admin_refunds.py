"""ADM3 — refunds: stripe client (mocked HTTP) + admin refund endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest

import app.routes.admin as admin_mod
import app.services.stripe_client as sc
from app.models.dentiva_staff import DentivaStaff
from app.models.invoice import Invoice
from app.models.user import User
from tests.conftest import seed_practice


class _Cfg:
    stripe_secret_key = "sk_test_x"


async def test_refund_invoice_two_step(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.content.decode()))
        if req.url.path == "/v1/invoices/in_1":
            return httpx.Response(200, json={"id": "in_1", "payment_intent": "pi_9"})
        return httpx.Response(200, json={"id": "re_1", "status": "succeeded"})

    r = await sc.refund_invoice("in_1", 500, transport=httpx.MockTransport(handler))
    assert r["id"] == "re_1"
    assert calls[0][:2] == ("GET", "/v1/invoices/in_1")
    assert calls[1][:2] == ("POST", "/v1/refunds")
    assert "payment_intent=pi_9" in calls[1][2] and "amount=500" in calls[1][2]


async def test_refund_unpaid_invoice_raises(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "in_2", "payment_intent": None})

    with pytest.raises(sc.StripeError, match="no payment"):
        await sc.refund_invoice("in_2", transport=httpx.MockTransport(handler))


# ── admin endpoint ───────────────────────────────────────────────────────────
async def _internal(db_session, *, clerk_id, role):
    u = User(id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
             email=f"{clerk_id}@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": "org_internal"}


async def _invoice(db_session, practice_id, *, stripe_id="in_test", status="paid",
                   amount=24900) -> Invoice:
    inv = Invoice(id=uuid.uuid4(), practice_id=practice_id, stripe_invoice_id=stripe_id,
                  amount_cents=amount, status=status, paid_at=datetime.now(tz=UTC))
    db_session.add(inv)
    await db_session.commit()
    return inv


async def test_full_refund_flips_status(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_r1", role="finance")  # MANAGE_BILLING_ALL
    practice, _ = await seed_practice(db_session, name="RefCo",
                                      clerk_org_id="o_ref", clerk_user_id="u_ref")
    inv = await _invoice(db_session, practice.id)
    refunded = {}

    async def _fake_refund(stripe_id, amount=None, **_kw):
        refunded["id"] = stripe_id
        refunded["amount"] = amount
        return {"id": "re_x", "status": "succeeded"}
    monkeypatch.setattr(admin_mod, "refund_invoice", _fake_refund)

    r = await client.post(f"/api/admin/invoices/{inv.id}/refund",
                          headers=_h("fin_r1"), json={})
    assert r.status_code == 200
    assert r.json()["status"] == "refunded"
    assert refunded == {"id": "in_test", "amount": None}  # full refund

    # already refunded → 409 (can't refund twice)
    r2 = await client.post(f"/api/admin/invoices/{inv.id}/refund",
                           headers=_h("fin_r1"), json={})
    assert r2.status_code == 409


async def test_partial_refund_and_bounds(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_r2", role="finance")
    practice, _ = await seed_practice(db_session, name="RefCo2",
                                      clerk_org_id="o_ref2", clerk_user_id="u_ref2")
    inv = await _invoice(db_session, practice.id, stripe_id="in_p", amount=10000)

    async def _fake_refund(stripe_id, amount=None, **_kw):
        return {"id": "re_p", "status": "succeeded"}
    monkeypatch.setattr(admin_mod, "refund_invoice", _fake_refund)

    # over-refund → 422
    over = await client.post(f"/api/admin/invoices/{inv.id}/refund",
                             headers=_h("fin_r2"), json={"amount_cents": 20000})
    assert over.status_code == 422
    # partial → partially_refunded
    part = await client.post(f"/api/admin/invoices/{inv.id}/refund",
                             headers=_h("fin_r2"), json={"amount_cents": 2500})
    assert part.status_code == 200
    assert part.json()["status"] == "partially_refunded"


async def test_refund_guards(client, db_session):
    await _internal(db_session, clerk_id="fin_r3", role="finance")
    practice, _ = await seed_practice(db_session, name="RefCo3",
                                      clerk_org_id="o_ref3", clerk_user_id="u_ref3")
    local = await _invoice(db_session, practice.id, stripe_id=None)  # no Stripe payment
    r = await client.post(f"/api/admin/invoices/{local.id}/refund",
                          headers=_h("fin_r3"), json={})
    assert r.status_code == 409
    # missing invoice → 404
    r404 = await client.post(f"/api/admin/invoices/{uuid.uuid4()}/refund",
                             headers=_h("fin_r3"), json={})
    assert r404.status_code == 404


async def test_refund_denied_wrong_roles(client, db_session):
    await _internal(db_session, clerk_id="sales_r", role="sales")   # no MANAGE_BILLING_ALL
    await _internal(db_session, clerk_id="sup_r", role="support")
    for who in ("sales_r", "sup_r", "random_clinic"):
        r = await client.post(f"/api/admin/invoices/{uuid.uuid4()}/refund",
                              headers=_h(who), json={})
        assert r.status_code in (401, 403), who


async def test_admin_lists_clinic_invoices(client, db_session):
    await _internal(db_session, clerk_id="fin_r4", role="finance")  # VIEW_BILLING_ALL
    practice, _ = await seed_practice(db_session, name="RefCo4",
                                      clerk_org_id="o_ref4", clerk_user_id="u_ref4")
    await _invoice(db_session, practice.id, stripe_id="in_a")
    await _invoice(db_session, practice.id, stripe_id="in_b", status="open")
    r = await client.get(f"/api/admin/clinics/{practice.id}/invoices",
                         headers=_h("fin_r4"))
    assert r.status_code == 200 and len(r.json()) == 2
