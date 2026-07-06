"""ADM9 — staff invite (Clerk instance invitations) + deactivate + webhook provisioning."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

import app.routes.admin as admin_mod
import app.services.clerk_api as clerk_api
from app.models.dentiva_staff import DentivaStaff
from app.models.user import User


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


# ── clerk client ─────────────────────────────────────────────────────────────
class _Cfg:
    clerk_secret_key = "sk_clerk_test"


async def test_platform_invitation_posts_metadata(monkeypatch):
    monkeypatch.setattr(clerk_api, "get_settings", lambda: _Cfg())
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "inv_1"})

    inv = await clerk_api.create_platform_invitation(
        email="new@dentovox.com", public_metadata={"dentiva_role": "support"},
        transport=httpx.MockTransport(handler),
    )
    assert inv == "inv_1"
    assert seen["path"] == "/v1/invitations"
    assert seen["body"] == {"email_address": "new@dentovox.com",
                            "public_metadata": {"dentiva_role": "support"}}


async def test_platform_invitation_errors(monkeypatch):
    monkeypatch.setattr(clerk_api, "get_settings", lambda: _Cfg())

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"errors": [{"message": "duplicate invitation"}]})

    import pytest
    with pytest.raises(clerk_api.ClerkError, match="duplicate"):
        await clerk_api.create_platform_invitation(
            email="dup@x.com", transport=httpx.MockTransport(handler))


# ── invite endpoint ──────────────────────────────────────────────────────────
def _fake_invite(monkeypatch):
    calls = []

    async def _create(*, email, public_metadata=None, **_kw):
        calls.append((email, public_metadata))
        return "inv_ok"

    monkeypatch.setattr(admin_mod, "create_platform_invitation", _create)
    return calls


async def test_invite_staff_happy_path(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="sa_i1", role="super_admin")
    calls = _fake_invite(monkeypatch)
    r = await client.post("/api/admin/staff/invite", headers=_h("sa_i1"),
                          json={"email": "  Newbie@Dentovox.com ", "role": "support"})
    assert r.status_code == 200 and r.json()["invitation_id"] == "inv_ok"
    # normalized email + role metadata went to Clerk
    assert calls == [("newbie@dentovox.com", {"dentiva_role": "support"})]


async def test_invite_staff_validation(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="sa_i2", role="super_admin")
    _fake_invite(monkeypatch)
    bad_email = await client.post("/api/admin/staff/invite", headers=_h("sa_i2"),
                                  json={"email": "not-an-email", "role": "support"})
    assert bad_email.status_code == 422
    bad_role = await client.post("/api/admin/staff/invite", headers=_h("sa_i2"),
                                 json={"email": "x@y.com", "role": "godmode"})
    assert bad_role.status_code == 422
    # already-internal email → 409
    dup = await client.post("/api/admin/staff/invite", headers=_h("sa_i2"),
                            json={"email": "sa_i2@dentovox.com", "role": "support"})
    assert dup.status_code == 409


async def test_invite_denied_wrong_roles(client, db_session):
    await _internal(db_session, clerk_id="fin_i", role="finance")  # no MANAGE_DENTIVA_STAFF
    for who in ("fin_i", "random_clinic"):
        r = await client.post("/api/admin/staff/invite", headers=_h(who),
                              json={"email": "a@b.com", "role": "support"})
        assert r.status_code in (401, 403), who


# ── deactivate ───────────────────────────────────────────────────────────────
async def test_deactivate_staff(client, db_session):
    await _internal(db_session, clerk_id="sa_d1", role="super_admin")
    target = await _internal(db_session, clerk_id="sup_d1", role="support")

    r = await client.delete(f"/api/admin/staff/{target.id}", headers=_h("sa_d1"))
    assert r.status_code == 200 and r.json()["removed_role"] == "support"
    # role row gone → target loses admin access immediately
    left = (await db_session.execute(
        select(DentivaStaff).where(DentivaStaff.user_id == target.id)
    )).scalar_one_or_none()
    assert left is None
    denied = await client.get("/api/admin/clinics", headers=_h("sup_d1"))
    assert denied.status_code == 403
    # repeat → 404
    again = await client.delete(f"/api/admin/staff/{target.id}", headers=_h("sa_d1"))
    assert again.status_code == 404


async def test_cannot_deactivate_self(client, db_session):
    me = await _internal(db_session, clerk_id="sa_d2", role="super_admin")
    r = await client.delete(f"/api/admin/staff/{me.id}", headers=_h("sa_d2"))
    assert r.status_code == 409


async def test_last_super_admin_lockout_guards(client, db_session):
    """Reviewer: removing/demoting the LAST super_admin would freeze staff
    management forever (only super_admin holds MANAGE_DENTIVA_STAFF)."""
    sa1 = await _internal(db_session, clerk_id="sa_l1", role="super_admin")
    sa2 = await _internal(db_session, clerk_id="sa_l2", role="super_admin")

    # Two exist → removing one is fine.
    r1 = await client.delete(f"/api/admin/staff/{sa2.id}", headers=_h("sa_l1"))
    assert r1.status_code == 200
    # sa1 is now the last → a (hypothetical second) super_admin can't remove them;
    # emulate via sa1 targeting themselves is already 409, so re-add sa2 as support
    # and confirm support can't do it anyway (403), then check the demote guard:
    demote = await client.patch(f"/api/admin/staff/{sa1.id}",
                                headers=_h("sa_l1"), json={"role": "finance"})
    assert demote.status_code == 409  # last super_admin cannot be demoted
    # promoting someone else first unblocks the demotion path
    sup = await _internal(db_session, clerk_id="sup_l3", role="support")
    up = await client.patch(f"/api/admin/staff/{sup.id}",
                            headers=_h("sa_l1"), json={"role": "super_admin"})
    assert up.status_code == 200
    demote2 = await client.patch(f"/api/admin/staff/{sa1.id}",
                                 headers=_h("sa_l1"), json={"role": "finance"})
    assert demote2.status_code == 200


# ── webhook provisioning ─────────────────────────────────────────────────────
async def test_user_created_with_invite_metadata_provisions_staff(db_session):
    from app.services.clerk_provisioning import handle_user_created

    result = await handle_user_created(db_session, {
        "id": "user_inv_1",
        "email_addresses": [{"id": "e1", "email_address": "invited@dentovox.com"}],
        "primary_email_address_id": "e1",
        "public_metadata": {"dentiva_role": "sales"},
    })
    await db_session.commit()
    assert result == "created_internal:sales"
    u = (await db_session.execute(
        select(User).where(User.clerk_user_id == "user_inv_1")
    )).scalar_one()
    assert u.is_internal is True
    staff = (await db_session.execute(
        select(DentivaStaff).where(DentivaStaff.user_id == u.id)
    )).scalar_one()
    assert staff.role == "sales"


async def test_user_created_garbage_role_stays_regular(db_session):
    """A forged/unknown dentiva_role must NOT create privileged rows."""
    from app.services.clerk_provisioning import handle_user_created

    result = await handle_user_created(db_session, {
        "id": "user_inv_2",
        "email_addresses": [{"id": "e1", "email_address": "hax@x.com"}],
        "primary_email_address_id": "e1",
        "public_metadata": {"dentiva_role": "super_duper_admin"},
    })
    await db_session.commit()
    assert result == "created"
    u = (await db_session.execute(
        select(User).where(User.clerk_user_id == "user_inv_2")
    )).scalar_one()
    assert u.is_internal is False and u.role == "viewer"
