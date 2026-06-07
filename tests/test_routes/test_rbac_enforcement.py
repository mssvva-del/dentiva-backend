"""Phase C — RBAC enforcement on existing clinic module endpoints.

The permission check runs as a dependency BEFORE the handler body, so we can
assert the gate cleanly with a non-existent resource id:
  * a role WITHOUT the permission → 403 (blocked before any lookup)
  * a role WITH the permission     → NOT 403 (passes the gate; 404 because the
    fabricated id doesn't exist, which proves the gate let it through)

This is the real security boundary; the frontend useCan() gates are cosmetic.
"""

from __future__ import annotations

import uuid

from app.models.user import User
from tests.conftest import seed_practice

ORG = "org_rbac"


async def _seed_roles(db_session):
    """One practice (owner) + a viewer, staff, and manager in it."""
    practice, owner = await seed_practice(
        db_session, name="RBAC Co", clerk_org_id=ORG, clerk_user_id="u_owner"
    )
    for clerk_id, role in [
        ("u_viewer", "viewer"), ("u_staff", "staff"), ("u_manager", "manager"),
    ]:
        db_session.add(User(
            id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=practice.id,
            email=f"{clerk_id}@rbac.com", role=role,
        ))
    await db_session.commit()
    return practice


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": ORG}


_RANDOM = uuid.uuid4()  # a resource id that never exists


# Each: (method, path, denied_role, allowed_role)
# denied_role lacks the permission → 403; allowed_role has it → not 403.
async def test_bookings_status_requires_manage_appointments(client, db_session):
    await _seed_roles(db_session)
    deny = await client.patch(f"/api/bookings/{_RANDOM}/status",
                              headers=_h("u_viewer"), json={"status": "confirmed"})
    assert deny.status_code == 403
    ok = await client.patch(f"/api/bookings/{_RANDOM}/status",
                            headers=_h("u_staff"), json={"status": "confirmed"})
    assert ok.status_code != 403


async def test_callbacks_update_requires_manage_calls(client, db_session):
    await _seed_roles(db_session)
    deny = await client.patch(f"/api/callbacks/{_RANDOM}",
                              headers=_h("u_viewer"), json={"status": "handled"})
    assert deny.status_code == 403
    ok = await client.patch(f"/api/callbacks/{_RANDOM}",
                            headers=_h("u_staff"), json={"status": "handled"})
    assert ok.status_code != 403


async def test_patient_recall_sms_requires_send_sms(client, db_session):
    await _seed_roles(db_session)
    deny = await client.post(f"/api/patients/{_RANDOM}/recall-sms", headers=_h("u_viewer"))
    assert deny.status_code == 403
    ok = await client.post(f"/api/patients/{_RANDOM}/recall-sms", headers=_h("u_staff"))
    assert ok.status_code != 403


async def test_waitlist_update_requires_manage_appointments(client, db_session):
    await _seed_roles(db_session)
    deny = await client.patch(f"/api/waitlist/{_RANDOM}",
                              headers=_h("u_viewer"), json={"status": "scheduled"})
    assert deny.status_code == 403
    ok = await client.patch(f"/api/waitlist/{_RANDOM}",
                            headers=_h("u_staff"), json={"status": "scheduled"})
    assert ok.status_code != 403


async def test_practice_update_requires_manage_settings(client, db_session):
    await _seed_roles(db_session)
    # staff does NOT have MANAGE_SETTINGS (manager+ only) → 403.
    assert (await client.patch("/api/practice/me", headers=_h("u_viewer"),
                               json={"name": "X"})).status_code == 403
    assert (await client.patch("/api/practice/me", headers=_h("u_staff"),
                               json={"name": "X"})).status_code == 403
    # manager has it → applies the update (200).
    ok = await client.patch("/api/practice/me", headers=_h("u_manager"),
                            json={"name": "Renamed Co"})
    assert ok.status_code == 200
    assert ok.json()["name"] == "Renamed Co"
