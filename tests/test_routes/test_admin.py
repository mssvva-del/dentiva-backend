"""Phase E — Dentiva admin API: internal-only, role-gated, audited, cross-tenant."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.dentiva_staff import DentivaStaff
from app.models.subscription import Subscription
from app.models.user import User
from tests.conftest import seed_practice

ORG = "org_admin"


async def _internal(db_session, *, clerk_id, role):
    """Create an internal Dentiva staff user with the given role."""
    u = User(
        id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
        email=f"{clerk_id}@dentiva.com", role="staff", is_internal=True,
    )
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()
    return u


def _h(user, org="org_internal"):
    # Internal users have no clinic org; the header org is irrelevant to admin gates.
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


# ── access control ───────────────────────────────────────────────────────────
async def test_clinic_user_blocked_from_admin(client, db_session):
    await seed_practice(db_session, name="ClinicA", clerk_org_id=ORG, clerk_user_id="clinic_owner")
    r = await client.get("/api/admin/clinics", headers=_h("clinic_owner", ORG))
    assert r.status_code == 403  # not internal


async def test_super_admin_lists_clinics(client, db_session):
    await seed_practice(db_session, name="ClinicB", clerk_org_id="org_b", clerk_user_id="ob")
    await _internal(db_session, clerk_id="sa1", role="super_admin")
    r = await client.get("/api/admin/clinics", headers=_h("sa1"))
    assert r.status_code == 200
    assert any(c["name"] == "ClinicB" for c in r.json())


async def test_role_boundary_finance_vs_staff_mgmt(client, db_session):
    await _internal(db_session, clerk_id="fin1", role="finance")
    # finance HAS VIEW_REVENUE
    assert (await client.get("/api/admin/revenue", headers=_h("fin1"))).status_code == 200
    # finance LACKS MANAGE_DENTIVA_STAFF
    assert (await client.get("/api/admin/staff", headers=_h("fin1"))).status_code == 403


async def test_sales_cannot_view_revenue(client, db_session):
    await _internal(db_session, clerk_id="sal1", role="sales")
    # sales HAS VIEW_ALL_CLINICS
    assert (await client.get("/api/admin/clinics", headers=_h("sal1"))).status_code == 200
    # sales LACKS VIEW_REVENUE
    assert (await client.get("/api/admin/revenue", headers=_h("sal1"))).status_code == 403


# ── clinic detail is audited ─────────────────────────────────────────────────
async def test_clinic_detail_writes_audit(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="ClinicC", clerk_org_id="org_c", clerk_user_id="oc"
    )
    await _internal(db_session, clerk_id="sa2", role="super_admin")
    r = await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sa2"))
    assert r.status_code == 200
    assert r.json()["name"] == "ClinicC"
    # an audit row for the cross-tenant view exists
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "admin_view_clinic")
    )).scalars().all()
    assert any(a.resource_id == practice.id for a in rows)


# ── subscription override / pilot ────────────────────────────────────────────
async def test_override_subscription_sets_pilot(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="ClinicD", clerk_org_id="org_d", clerk_user_id="od"
    )
    await _internal(db_session, clerk_id="sa3", role="super_admin")
    r = await client.patch(
        f"/api/admin/clinics/{practice.id}/subscription", headers=_h("sa3"),
        json={"plan": "practice", "status": "pilot", "mrr_cents": 0},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pilot" and r.json()["plan"] == "practice"
    sub = (await db_session.execute(
        select(Subscription).where(Subscription.practice_id == practice.id)
    )).scalar_one()
    assert sub.status == "pilot"


# ── feature flags ────────────────────────────────────────────────────────────
async def test_feature_flag_upsert(client, db_session):
    await _internal(db_session, clerk_id="eng1", role="engineer")
    r = await client.put("/api/admin/feature-flags", headers=_h("eng1"),
                         json={"flag_key": "beta_calendar", "enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    # toggling again updates in place (idempotent upsert)
    r2 = await client.put("/api/admin/feature-flags", headers=_h("eng1"),
                          json={"flag_key": "beta_calendar", "enabled": False})
    assert r2.json()["enabled"] is False
    listed = await client.get("/api/admin/feature-flags", headers=_h("eng1"))
    assert sum(1 for f in listed.json() if f["flag_key"] == "beta_calendar") == 1


# ── audit log viewer ─────────────────────────────────────────────────────────
async def test_audit_viewer_returns_rows(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="ClinicE", clerk_org_id="org_e", clerk_user_id="oe"
    )
    await _internal(db_session, clerk_id="sup1", role="support")
    # generate an auditable event (support can view clinic detail + audit logs)
    await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sup1"))
    r = await client.get("/api/admin/audit-logs", headers=_h("sup1"))
    assert r.status_code == 200
    assert len(r.json()) >= 1
