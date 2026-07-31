"""POST /api/onboarding/phone/provision — the endpoint that spends real money.

This buys a Dentovox number from Retell. The route guards the buy with a
``SELECT ... FOR UPDATE`` so two near-simultaneous requests for the same
practice (a double-click, or React re-running an effect) can't both see
``ai_phone_number IS NULL`` and both purchase — that would mean a permanent
second monthly bill for a number nobody uses. No prior test exercised this
endpoint at all; the lock existed but was never proven.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.practice import Practice
from app.services.telephony import provision as prov
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def test_provision_endpoint_happy_path(client, db_session, monkeypatch):
    practice, _ = await seed_practice(
        db_session, name="Provision Dental", clerk_org_id="org_prov1", clerk_user_id="user_prov1"
    )

    async def _fake_provision(p, *, transport=None):
        return "+17185551234"

    monkeypatch.setattr(prov, "provision_number_for_practice", _fake_provision)

    resp = await client.post(
        "/api/onboarding/phone/provision", headers=_auth("org_prov1", "user_prov1")
    )
    assert resp.status_code == 200
    assert resp.json()["ai_phone_number"] == "+17185551234"

    await db_session.commit()
    row = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    assert row.ai_phone_number == "+17185551234"

    audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.practice_id == practice.id, AuditLog.action == "number_provisioned"
            )
        )
    ).scalars().all()
    assert len(audits) == 1, "the purchase must be auditable"


async def test_provision_endpoint_is_idempotent_on_repeat_call(client, db_session, monkeypatch):
    """Doc'd behaviour: 'a practice that already has one gets it back unchanged'.
    A second click after the number is already set must NOT call the provider
    again — that would buy (and bill for) a second number."""
    practice, _ = await seed_practice(
        db_session, name="Repeat Dental", clerk_org_id="org_prov2", clerk_user_id="user_prov2"
    )
    calls = 0

    async def _fake_provision(p, *, transport=None):
        nonlocal calls
        calls += 1
        return "+17185555678"

    monkeypatch.setattr(prov, "provision_number_for_practice", _fake_provision)

    r1 = await client.post(
        "/api/onboarding/phone/provision", headers=_auth("org_prov2", "user_prov2")
    )
    r2 = await client.post(
        "/api/onboarding/phone/provision", headers=_auth("org_prov2", "user_prov2")
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["ai_phone_number"] == r2.json()["ai_phone_number"]
    assert calls == 1, "the provider must be called exactly once across both requests"


async def test_concurrent_double_click_buys_exactly_one_number(client, db_session, monkeypatch):
    """The real race: two requests for the SAME practice arrive close enough
    together that both could read ai_phone_number IS NULL before either writes.
    Without the row lock this buys two numbers — one of them forever unused and
    forever billed. The provider mock sleeps to widen the window past the point
    where the second request's FOR UPDATE would have to block on the first."""
    practice, _ = await seed_practice(
        db_session, name="Race Dental", clerk_org_id="org_prov3", clerk_user_id="user_prov3"
    )
    call_count = 0

    async def _slow_provision(p, *, transport=None):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.15)  # hold the FOR UPDATE lock while "talking to Retell"
        return f"+1718555{call_count:04d}"

    monkeypatch.setattr(prov, "provision_number_for_practice", _slow_provision)

    h = _auth("org_prov3", "user_prov3")
    r1, r2 = await asyncio.gather(
        client.post("/api/onboarding/phone/provision", headers=h),
        client.post("/api/onboarding/phone/provision", headers=h),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    # Both callers must see the SAME number — the second one waited for the lock
    # and then took the already-provisioned value, never bought its own.
    assert r1.json()["ai_phone_number"] == r2.json()["ai_phone_number"]
    assert call_count == 1, (
        f"provider called {call_count} times — a real second number would have "
        "been purchased and billed forever"
    )

    await db_session.commit()
    row = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    assert row.ai_phone_number == r1.json()["ai_phone_number"]


async def test_provision_not_entitled_returns_402(client, db_session, monkeypatch):
    practice, _ = await seed_practice(
        db_session, name="Trial Dental", clerk_org_id="org_prov4", clerk_user_id="user_prov4"
    )
    p = (await db_session.execute(select(Practice).where(Practice.id == practice.id))).scalar_one()
    p.status = "onboarding"  # not in PROVISIONABLE_STATUSES
    await db_session.commit()

    async def _refuse(p, *, transport=None):
        raise prov.NotEntitledToNumber("not entitled")

    monkeypatch.setattr(prov, "provision_number_for_practice", _refuse)

    resp = await client.post(
        "/api/onboarding/phone/provision", headers=_auth("org_prov4", "user_prov4")
    )
    assert resp.status_code == 402


async def test_provision_provider_error_returns_502_and_saves_nothing(
    client, db_session, monkeypatch
):
    practice, _ = await seed_practice(
        db_session, name="Flaky Dental", clerk_org_id="org_prov5", clerk_user_id="user_prov5"
    )

    async def _boom(p, *, transport=None):
        from app.services.retell_admin import RetellError
        raise RetellError("provider down", status_code=502)

    monkeypatch.setattr(prov, "provision_number_for_practice", _boom)

    resp = await client.post(
        "/api/onboarding/phone/provision", headers=_auth("org_prov5", "user_prov5")
    )
    assert resp.status_code == 502

    await db_session.commit()
    row = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    assert row.ai_phone_number is None, "a failed purchase must not be recorded as one"


async def test_provision_requires_manage_settings(client, db_session):
    await seed_practice(
        db_session, name="Staff Dental", clerk_org_id="org_prov6", clerk_user_id="owner_prov6"
    )
    # A staff-role caller (no MANAGE_SETTINGS) must not be able to spend the
    # practice's money on a phone number.
    from app.models.user import User
    staff = User(
        id=uuid.uuid4(),
        clerk_user_id="staff_prov6",
        practice_id=(
            await db_session.execute(
                select(Practice.id).where(Practice.name == "Staff Dental")
            )
        ).scalar_one(),
        email="staff_prov6@example.com",
        role="staff",
    )
    db_session.add(staff)
    await db_session.commit()

    resp = await client.post(
        "/api/onboarding/phone/provision", headers=_auth("org_prov6", "staff_prov6")
    )
    assert resp.status_code == 403
