"""Phase B3 — team management: invitations, role changes, last-owner guard,
RBAC, and webhook invitation reconciliation.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.models.invitation import Invitation
from app.models.user import User
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _member(db, practice_id, *, clerk_id, email, role):
    db.add(User(
        id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=practice_id,
        email=email, role=role,
    ))
    await db.commit()


# ── members + invitations happy path ─────────────────────────────────────────
async def test_list_members_marks_self(client, db_session):
    await seed_practice(db_session, name="T1", clerk_org_id="org_t1", clerk_user_id="user_t1")
    r = await client.get("/api/team/members", headers=_auth("org_t1", "user_t1"))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["role"] == "owner"
    assert body[0]["is_self"] is True


async def test_invite_then_list_then_revoke(client, db_session):
    await seed_practice(db_session, name="T2", clerk_org_id="org_t2", clerk_user_id="user_t2")
    h = _auth("org_t2", "user_t2")

    r = await client.post("/api/team/invitations", headers=h,
                          json={"email": "New.Hire@clinic.com", "role": "staff"})
    assert r.status_code == 201
    inv = r.json()
    assert inv["email"] == "new.hire@clinic.com"  # normalized
    assert inv["status"] == "pending"

    r = await client.get("/api/team/invitations", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = await client.post(f"/api/team/invitations/{inv['id']}/revoke", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"

    # revoking again → 422
    r = await client.post(f"/api/team/invitations/{inv['id']}/revoke", headers=h)
    assert r.status_code == 422


async def test_duplicate_pending_invite_conflict(client, db_session):
    await seed_practice(db_session, name="T3", clerk_org_id="org_t3", clerk_user_id="user_t3")
    h = _auth("org_t3", "user_t3")
    await client.post("/api/team/invitations", headers=h,
                      json={"email": "dup@clinic.com", "role": "staff"})
    r = await client.post("/api/team/invitations", headers=h,
                          json={"email": "dup@clinic.com", "role": "manager"})
    assert r.status_code == 409


async def test_invite_existing_member_conflict(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="T4", clerk_org_id="org_t4", clerk_user_id="user_t4"
    )
    await _member(db_session, practice.id, clerk_id="user_t4b",
                  email="member@clinic.com", role="staff")
    h = _auth("org_t4", "user_t4")
    r = await client.post("/api/team/invitations", headers=h,
                          json={"email": "member@clinic.com", "role": "viewer"})
    assert r.status_code == 409


async def test_invalid_email_rejected(client, db_session):
    await seed_practice(db_session, name="T5", clerk_org_id="org_t5", clerk_user_id="user_t5")
    h = _auth("org_t5", "user_t5")
    r = await client.post("/api/team/invitations", headers=h,
                          json={"email": "not-an-email", "role": "staff"})
    assert r.status_code == 400  # validation envelope


# ── role changes + last-owner guard ──────────────────────────────────────────
async def test_change_role_ok(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="T6", clerk_org_id="org_t6", clerk_user_id="user_t6"
    )
    await _member(db_session, practice.id, clerk_id="user_t6b",
                  email="m@clinic.com", role="manager")
    target = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_t6b"))
    ).scalar_one()
    h = _auth("org_t6", "user_t6")
    r = await client.patch(f"/api/team/members/{target.id}", headers=h,
                           json={"role": "viewer"})
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"


async def test_cannot_demote_last_owner(client, db_session):
    practice, owner = await seed_practice(
        db_session, name="T7", clerk_org_id="org_t7", clerk_user_id="user_t7"
    )
    h = _auth("org_t7", "user_t7")
    r = await client.patch(f"/api/team/members/{owner.id}", headers=h,
                           json={"role": "manager"})
    assert r.status_code == 422


async def test_cannot_remove_last_owner(client, db_session):
    practice, owner = await seed_practice(
        db_session, name="T8", clerk_org_id="org_t8", clerk_user_id="user_t8"
    )
    h = _auth("org_t8", "user_t8")
    r = await client.delete(f"/api/team/members/{owner.id}", headers=h)
    assert r.status_code == 422


async def test_remove_member_soft_disables(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="T9", clerk_org_id="org_t9", clerk_user_id="user_t9"
    )
    await _member(db_session, practice.id, clerk_id="user_t9b",
                  email="bye@clinic.com", role="staff")
    target = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_t9b"))
    ).scalar_one()
    h = _auth("org_t9", "user_t9")
    r = await client.delete(f"/api/team/members/{target.id}", headers=h)
    assert r.status_code == 200
    await db_session.refresh(target)
    assert target.status == "disabled"


# ── RBAC ─────────────────────────────────────────────────────────────────────
async def test_non_manager_blocked(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="T10", clerk_org_id="org_t10", clerk_user_id="user_t10"
    )
    await _member(db_session, practice.id, clerk_id="user_t10_staff",
                  email="s@clinic.com", role="staff")
    h = _auth("org_t10", "user_t10_staff")
    r = await client.get("/api/team/members", headers=h)
    assert r.status_code == 403


# ── webhook invitation reconciliation ────────────────────────────────────────
async def test_membership_webhook_honors_invited_role(client, db_session):
    await seed_practice(
        db_session, name="T11", clerk_org_id="org_t11", clerk_user_id="user_t11"
    )
    h = _auth("org_t11", "user_t11")
    # Owner invites someone as MANAGER (Clerk would map them to 'member'=staff,
    # but our invited role must win).
    await client.post("/api/team/invitations", headers=h,
                      json={"email": "invitee@clinic.com", "role": "manager"})

    # Clerk fires membership.created for that email as a plain member.
    body = json.dumps({
        "type": "organizationMembership.created",
        "data": {
            "organization": {"id": "org_t11"},
            "public_user_data": {"user_id": "user_invitee", "identifier": "invitee@clinic.com"},
            "role": "org:member",
        },
    }).encode()
    r = await client.post("/webhooks/clerk", content=body)
    assert r.status_code == 200

    u = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_invitee"))
    ).scalar_one()
    assert u.role == "manager"  # invited role won over Clerk's 'member'

    inv = (
        await db_session.execute(
            select(Invitation).where(Invitation.email == "invitee@clinic.com")
        )
    ).scalar_one()
    assert inv.status == "accepted"
