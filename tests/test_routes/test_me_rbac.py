"""Phase A4 — RBAC end-to-end: /api/me resolves correct permissions per role,
and require_permission / require_admin_permission enforce the boundary.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.auth import permissions as perms
from app.auth.admin import AdminContext, require_admin_permission
from app.auth.permissions import require_permission
from app.models.dentiva_staff import DentivaStaff
from app.models.user import User
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


# ── /api/me — clinic roles ──────────────────────────────────────────────────
async def test_me_owner_resolves_owner_permissions(client, db_session):
    await seed_practice(
        db_session, name="Me Owner", clerk_org_id="org_me1", clerk_user_id="user_me1"
    )
    resp = await client.get("/api/me", headers=_auth("org_me1", "user_me1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_internal"] is False
    assert body["role"] == "owner"
    assert set(body["permissions"]) == set(perms.clinic_permissions_for("owner"))


async def test_me_viewer_is_read_only(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Me Viewer", clerk_org_id="org_me2", clerk_user_id="user_me2_owner"
    )
    # Add a viewer user to the same practice.
    db_session.add(User(
        id=uuid.uuid4(), clerk_user_id="user_me2_viewer", practice_id=practice.id,
        email="v@x.com", role="viewer",
    ))
    await db_session.commit()

    resp = await client.get("/api/me", headers=_auth("org_me2", "user_me2_viewer"))
    assert resp.status_code == 200
    p = set(resp.json()["permissions"])
    assert perms.VIEW_DASHBOARD in p
    assert perms.MANAGE_BILLING not in p and perms.SEND_SMS not in p


# ── /api/me — internal Dentiva staff ────────────────────────────────────────
async def test_me_internal_super_admin(client, db_session):
    await seed_practice(
        db_session, name="Me Internal", clerk_org_id="org_me3", clerk_user_id="user_me3_owner"
    )
    internal = User(
        id=uuid.uuid4(), clerk_user_id="user_me3_admin", practice_id=None,
        email="boss@dentiva.com", role="staff", is_internal=True,
    )
    db_session.add(internal)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=internal.id, role="super_admin"))
    await db_session.commit()

    resp = await client.get("/api/me", headers=_auth("org_me3", "user_me3_admin"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_internal"] is True
    assert body["staff_role"] == "super_admin"
    assert body["practice_id"] is None
    assert set(body["permissions"]) == set(perms.ADMIN_PERMISSIONS)


# ── require_permission dependency (security boundary) ────────────────────────
async def test_require_permission_allows_and_denies():
    dep = require_permission(perms.MANAGE_BILLING)
    owner = User(clerk_user_id="x", email="o@x", role="owner")
    staff = User(clerk_user_id="y", email="s@x", role="staff")
    assert await dep(user=owner) is owner          # owner has MANAGE_BILLING
    with pytest.raises(HTTPException) as ei:
        await dep(user=staff)                       # staff does not
    assert ei.value.status_code == 403


async def test_require_admin_permission_blocks_clinic_user(db_session):
    # A non-internal user must never pass an admin-permission gate.
    # require_admin_permission nests require_internal; a clinic user can't even
    # construct an AdminContext — assert the internal gate rejects it first.
    clinic_user = User(clerk_user_id="z", email="c@x", role="owner", is_internal=False)
    from app.auth.admin import require_internal

    internal_dep = require_internal()
    with pytest.raises(HTTPException) as ei:
        await internal_dep(user=clinic_user)
    assert ei.value.status_code == 403


async def test_require_admin_permission_checks_role():
    # An internal 'sales' role can't access a finance-only permission.
    dep = require_admin_permission(perms.VIEW_REVENUE)
    ctx = AdminContext(user=User(clerk_user_id="a", email="a@x", role="staff", is_internal=True),
                       staff_role="sales")
    with pytest.raises(HTTPException) as ei:
        await dep(ctx=ctx)
    assert ei.value.status_code == 403
