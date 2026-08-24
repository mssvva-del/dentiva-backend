"""Phase D — clinic billing API: plans, summary, checkout gating."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.billing.metering import record_call_usage
from app.models.practice import Practice
from app.models.user import User
from app.services.billing_service import create_or_update_subscription
from tests.conftest import seed_practice

ORG = "org_bill"


async def _roles(db_session):
    practice, owner = await seed_practice(
        db_session, name="Bill Co", clerk_org_id=ORG, clerk_user_id="b_owner"
    )
    for cid, role in [("b_manager", "manager"), ("b_staff", "staff")]:
        db_session.add(User(
            id=uuid.uuid4(), clerk_user_id=cid, practice_id=practice.id,
            email=f"{cid}@b.com", role=role,
        ))
    await db_session.commit()
    return practice


def _h(u):
    return {"X-Dev-Clerk-User-Id": u, "X-Dev-Clerk-Org-Id": ORG}


async def test_plans_visible_to_manager_not_staff(client, db_session):
    await _roles(db_session)
    ok = await client.get("/api/billing/plans", headers=_h("b_manager"))
    assert ok.status_code == 200
    keys = {p["key"] for p in ok.json()}
    assert keys == {"after_hours", "full_time", "growth", "multi"}
    # staff lacks VIEW_BILLING
    assert (await client.get("/api/billing/plans", headers=_h("b_staff"))).status_code == 403


async def test_summary_no_subscription_shows_zeros(client, db_session):
    await _roles(db_session)
    r = await client.get("/api/billing/summary", headers=_h("b_owner"))
    assert r.status_code == 200
    body = r.json()
    assert body["plan"] is None
    assert body["usage"]["minutes_used"] == 0
    assert body["usage"]["included_minutes"] == 1500  # after_hours default fallback
    assert body["invoices"] == []


async def test_summary_reflects_subscription_and_usage(client, db_session):
    practice = await _roles(db_session)
    # Give the practice a Practice-plan subscription + some usage this period.
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    await create_or_update_subscription(db_session, p, plan_key="full_time", status="active")
    await record_call_usage(db_session, practice.id, 600, now=datetime.now(UTC))  # 10 min
    await db_session.commit()

    r = await client.get("/api/billing/summary", headers=_h("b_owner"))
    body = r.json()
    assert body["plan"] == "full_time"
    assert body["included_minutes"] == 2500
    assert body["usage"]["minutes_used"] == 10
    assert body["usage"]["calls_count"] == 1


async def test_checkout_owner_only_and_503_without_keys(client, db_session):
    await _roles(db_session)
    payload = {"plan": "after_hours", "billing_cycle": "monthly"}
    # manager has VIEW but not MANAGE_BILLING → 403
    assert (await client.post("/api/billing/checkout", headers=_h("b_manager"),
                              json=payload)).status_code == 403
    # owner passes RBAC but Stripe isn't configured → 503 (clean, not a crash)
    r = await client.post("/api/billing/checkout", headers=_h("b_owner"), json=payload)
    assert r.status_code == 503


async def test_checkout_rejects_unknown_plan(client, db_session):
    await _roles(db_session)
    r = await client.post("/api/billing/checkout", headers=_h("b_owner"),
                          json={"plan": "enterprise", "billing_cycle": "monthly"})
    assert r.status_code == 422


async def test_growth_cannot_be_bought_while_outbound_is_dark(
    client, db_session, monkeypatch
):
    """Growth's whole pitch is outbound — reactivation, recalls. The engine is
    built, but with no outbound agent and number configured, no outbound call
    can be dialled. Selling the tier in that state is charging for a capability
    the clinic discovers as a silent nothing."""
    from app import config
    from tests.conftest import seed_practice

    await seed_practice(
        db_session, name="Gate Dental", clerk_org_id="org_gate1", clerk_user_id="u_gate1"
    )
    settings = config.get_settings()
    monkeypatch.setattr(settings, "retell_outbound_agent_id", "")
    monkeypatch.setattr(settings, "retell_from_number", "")

    r = await client.post(
        "/api/billing/checkout",
        headers={"X-Dev-Clerk-User-Id": "u_gate1", "X-Dev-Clerk-Org-Id": "org_gate1"},
        json={"plan": "growth", "billing_cycle": "monthly"},
    )
    assert r.status_code == 409
    assert "outbound" in r.json()["error"]["message"].lower()
