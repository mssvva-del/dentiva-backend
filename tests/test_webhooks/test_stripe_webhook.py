"""Phase D — Stripe webhook: signature verify + billing lifecycle reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.invoice import Invoice
from app.models.practice import Practice
from app.models.subscription import Subscription
from app.webhooks.stripe_verify import StripeVerificationError, verify
from tests.conftest import seed_practice

_SECRET = "whsec_stripe_test_123"


def _sig(secret: str, ts: str, body: bytes) -> str:
    payload = f"{ts}.".encode() + body
    h = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={h}"


# ── verifier unit ────────────────────────────────────────────────────────────
def test_stripe_verify_ok_and_tamper():
    body = b'{"a":1}'
    header = _sig(_SECRET, "1700000000", body)
    verify(secret=_SECRET, raw_body=body, signature_header=header, now=1700000001)
    with pytest.raises(StripeVerificationError):
        verify(secret=_SECRET, raw_body=b'{"a":2}', signature_header=header, now=1700000001)


def test_stripe_verify_stale():
    body = b"{}"
    header = _sig(_SECRET, "1700000000", body)
    with pytest.raises(StripeVerificationError):
        verify(secret=_SECRET, raw_body=body, signature_header=header, now=1700001000)


# ── lifecycle (dev mode, no secret → signature skipped) ──────────────────────
async def _event(t: str, obj: dict) -> bytes:
    return json.dumps({"type": t, "data": {"object": obj}}).encode()


async def test_checkout_completed_activates_subscription(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Stripe1", clerk_org_id="org_st1", clerk_user_id="u_st1"
    )
    body = await _event("checkout.session.completed", {
        "customer": "cus_123",
        "subscription": "sub_123",
        "metadata": {"practice_id": str(practice.id), "plan": "practice",
                     "billing_cycle": "monthly"},
    })
    r = await client.post("/webhooks/stripe", content=body)
    assert r.status_code == 200 and r.json()["result"] == "subscription_active"

    sub = (await db_session.execute(
        select(Subscription).where(Subscription.practice_id == practice.id)
    )).scalar_one()
    assert sub.plan == "practice" and sub.status == "active"
    # Reviewer: the legacy key is stored verbatim, but entitlements must resolve
    # from the catalog via the alias (practice → full_time: 2500 min / 15¢).
    assert sub.included_minutes == 2500
    assert sub.overage_cents_per_min == 15
    assert sub.stripe_subscription_id == "sub_123"
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    assert p.stripe_customer_id == "cus_123"


async def test_payment_failed_then_deleted_suspends(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Stripe2", clerk_org_id="org_st2", clerk_user_id="u_st2"
    )
    # Activate first.
    await client.post("/webhooks/stripe", content=await _event(
        "checkout.session.completed",
        {"customer": "cus_2", "subscription": "sub_2",
         "metadata": {"practice_id": str(practice.id), "plan": "starter"}},
    ))
    # Payment fails → past_due (grace, NOT suspended yet).
    r = await client.post("/webhooks/stripe", content=await _event(
        "invoice.payment_failed",
        {"customer": "cus_2", "subscription": "sub_2", "id": "in_1", "amount_due": 14900},
    ))
    assert r.json()["result"] == "past_due"
    sub = (await db_session.execute(
        select(Subscription).where(Subscription.practice_id == practice.id)
    )).scalar_one()
    assert sub.status == "past_due"

    # Stripe gives up → subscription deleted → suspend the practice.
    r = await client.post("/webhooks/stripe", content=await _event(
        "customer.subscription.deleted", {"id": "sub_2", "customer": "cus_2"},
    ))
    assert r.json()["result"] == "suspended"
    # Verify ground truth from a FRESH session — db_session has these rows cached
    # from earlier reads (expire_on_commit=False) and won't reflect the webhook's
    # commit otherwise.
    import app.db as _db
    async with _db.async_session_factory() as s2:
        sub2 = (await s2.execute(
            select(Subscription).where(Subscription.practice_id == practice.id)
        )).scalar_one()
        p2 = (await s2.execute(
            select(Practice).where(Practice.id == practice.id)
        )).scalar_one()
    assert p2.status == "suspended" and sub2.status == "suspended"


async def test_invoice_paid_records_and_reactivates(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Stripe3", clerk_org_id="org_st3", clerk_user_id="u_st3"
    )
    await client.post("/webhooks/stripe", content=await _event(
        "checkout.session.completed",
        {"customer": "cus_3", "subscription": "sub_3",
         "metadata": {"practice_id": str(practice.id), "plan": "starter"}},
    ))
    r = await client.post("/webhooks/stripe", content=await _event(
        "invoice.paid",
        {"customer": "cus_3", "subscription": "sub_3", "id": "in_3", "amount_paid": 14900},
    ))
    assert r.json()["result"] == "invoice_paid"
    inv = (await db_session.execute(
        select(Invoice).where(Invoice.practice_id == practice.id)
    )).scalar_one()
    assert inv.status == "paid" and inv.amount_cents == 14900


async def test_invoice_idempotent_on_redelivery(client, db_session):
    # Stripe redelivers webhooks — the SAME invoice.paid twice must yield ONE row.
    practice, _ = await seed_practice(
        db_session, name="Stripe4", clerk_org_id="org_st4", clerk_user_id="u_st4"
    )
    await client.post("/webhooks/stripe", content=await _event(
        "checkout.session.completed",
        {"customer": "cus_4", "subscription": "sub_4",
         "metadata": {"practice_id": str(practice.id), "plan": "starter"}},
    ))
    evt = await _event(
        "invoice.paid",
        {"customer": "cus_4", "subscription": "sub_4", "id": "in_dup", "amount_paid": 14900},
    )
    await client.post("/webhooks/stripe", content=evt)
    await client.post("/webhooks/stripe", content=evt)  # redelivery

    import app.db as _db
    async with _db.async_session_factory() as s2:
        rows = (await s2.execute(
            select(Invoice).where(Invoice.stripe_invoice_id == "in_dup")
        )).scalars().all()
    assert len(rows) == 1, "redelivered invoice must not duplicate"
    assert rows[0].status == "paid"


async def test_unknown_event_ignored(client):
    r = await client.post("/webhooks/stripe", content=await _event("payout.created", {}))
    assert r.status_code == 200 and r.json()["status"] == "ignored"


async def test_bad_signature_rejected_when_secret_set(client, db_session, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)
    get_settings.cache_clear()
    try:
        body = await _event("invoice.paid", {"id": "x"})
        r = await client.post("/webhooks/stripe", content=body,
                              headers={"stripe-signature": "t=1,v1=bad"})
        assert r.status_code == 401
    finally:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        get_settings.cache_clear()


async def _event_id(t: str, obj: dict, eid: str) -> bytes:
    # Like _event but with a Stripe event id, so the dispatcher's dedup engages.
    return json.dumps({"id": eid, "type": t, "data": {"object": obj}}).encode()


async def test_duplicate_event_id_is_ignored(client, db_session):
    # A redelivered checkout.session.completed must NOT re-run — a stale one
    # arriving after a suspension would otherwise reactivate service (audit #4).
    practice, _ = await seed_practice(
        db_session, name="StripeDup", clerk_org_id="org_dup", clerk_user_id="u_dup"
    )
    obj = {"customer": "cus_d", "subscription": "sub_d",
           "metadata": {"practice_id": str(practice.id), "plan": "practice",
                        "billing_cycle": "monthly"}}
    r1 = await client.post("/webhooks/stripe", content=await _event_id(
        "checkout.session.completed", obj, "evt_dup_1"))
    assert r1.json()["result"] == "subscription_active"

    r2 = await client.post("/webhooks/stripe", content=await _event_id(
        "checkout.session.completed", obj, "evt_dup_1"))  # same event id
    assert r2.json()["status"] == "duplicate"


async def test_invoice_keeps_its_receipt_across_redelivery(client, db_session):
    """A billing row of date + amount is not something a practice's bookkeeper can
    file. Stripe puts a hosted page and a PDF on every invoice and we were
    dropping both — and a later webhook for the same invoice can carry neither,
    so storing must not blank what the clinic already had."""
    practice, _ = await seed_practice(
        db_session, name="StripeDoc", clerk_org_id="org_stdoc", clerk_user_id="u_stdoc"
    )
    await client.post("/webhooks/stripe", content=await _event(
        "checkout.session.completed",
        {"customer": "cus_doc", "subscription": "sub_doc",
         "metadata": {"practice_id": str(practice.id), "plan": "starter"}},
    ))
    await client.post("/webhooks/stripe", content=await _event(
        "invoice.paid",
        {"customer": "cus_doc", "subscription": "sub_doc", "id": "in_doc",
         "amount_paid": 24900,
         "hosted_invoice_url": "https://invoice.stripe.com/i/in_doc",
         "invoice_pdf": "https://invoice.stripe.com/i/in_doc.pdf"},
    ))
    inv = (await db_session.execute(
        select(Invoice).where(Invoice.practice_id == practice.id)
    )).scalar_one()
    await db_session.refresh(inv)
    assert inv.invoice_pdf_url == "https://invoice.stripe.com/i/in_doc.pdf"
    assert inv.hosted_invoice_url == "https://invoice.stripe.com/i/in_doc"

    # Redelivery without the links must not take the receipt away.
    await client.post("/webhooks/stripe", content=await _event(
        "invoice.paid",
        {"customer": "cus_doc", "subscription": "sub_doc", "id": "in_doc",
         "amount_paid": 24900},
    ))
    await db_session.refresh(inv)
    assert inv.invoice_pdf_url == "https://invoice.stripe.com/i/in_doc.pdf"
