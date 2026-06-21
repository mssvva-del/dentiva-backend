"""S7 — explicit permission gate on /api/calls endpoints.

Every call-listing endpoint requires the clinic permission VIEW_CALLS. Roles in
the clinic world (owner/manager/staff/viewer) all have it; an unknown/empty role
is rejected with 403 (principle of least privilege).
"""

from __future__ import annotations

import uuid

from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _hdr(user_id: str, org_id: str) -> dict:
    return {"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id}


async def _seed_with_role(db_session, role: str):
    org_id, user_id = _uid("org_perm"), _uid("user_perm")
    practice, user = await seed_practice(
        db_session, name="Perm Test", clerk_org_id=org_id, clerk_user_id=user_id
    )
    user.role = role
    await db_session.commit()
    return org_id, user_id


async def test_viewer_role_can_list_calls(client, db_session):
    # viewer has VIEW_CALLS → 200.
    org_id, user_id = await _seed_with_role(db_session, "viewer")
    resp = await client.get("/api/calls", headers=_hdr(user_id, org_id))
    assert resp.status_code == 200


async def test_owner_role_can_list_calls(client, db_session):
    org_id, user_id = await _seed_with_role(db_session, "owner")
    resp = await client.get("/api/calls", headers=_hdr(user_id, org_id))
    assert resp.status_code == 200


async def test_unknown_role_forbidden_on_list(client, db_session):
    # Empty/unknown role lacks VIEW_CALLS → 403, not 200.
    org_id, user_id = await _seed_with_role(db_session, "")
    resp = await client.get("/api/calls", headers=_hdr(user_id, org_id))
    assert resp.status_code == 403


async def test_unknown_role_forbidden_on_active(client, db_session):
    org_id, user_id = await _seed_with_role(db_session, "")
    resp = await client.get("/api/calls/active", headers=_hdr(user_id, org_id))
    assert resp.status_code == 403


async def test_unknown_role_forbidden_on_detail(client, db_session):
    org_id, user_id = await _seed_with_role(db_session, "")
    resp = await client.get(
        f"/api/calls/{uuid.uuid4()}", headers=_hdr(user_id, org_id)
    )
    # Gate runs before the 404 lookup → 403.
    assert resp.status_code == 403
