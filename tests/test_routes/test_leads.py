"""ADM1 — lead inbox: public capture (POST /api/leads) + admin management."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.dentiva_staff import DentivaStaff
from app.models.lead import Lead
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


async def _count(db_session) -> int:
    return (await db_session.execute(select(func.count()).select_from(Lead))).scalar_one()


# ── public capture ────────────────────────────────────────────────────────
async def test_public_lead_created(client, db_session):
    r = await client.post("/api/leads", json={
        "name": "Dr. Kim", "email": "kim@brightsmiles.com", "phone": "305-555-0100",
        "clinic_name": "Bright Smiles", "message": "Want a demo",
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert await _count(db_session) == 1
    lead = (await db_session.execute(select(Lead))).scalar_one()
    assert lead.email == "kim@brightsmiles.com" and lead.status == "new"
    assert lead.source == "site"


async def test_honeypot_silently_dropped(client, db_session):
    r = await client.post("/api/leads", json={
        "email": "bot@spam.com", "website": "http://spam",  # honeypot filled
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert await _count(db_session) == 0  # not stored


async def test_no_contact_not_stored(client, db_session):
    r = await client.post("/api/leads", json={"name": "Nobody", "email": "not-an-email"})
    assert r.status_code == 200  # accepted quietly
    assert await _count(db_session) == 0  # no usable contact → nothing stored


# ── admin management ──────────────────────────────────────────────────────
async def test_admin_lists_and_updates_lead(client, db_session):
    await _internal(db_session, clerk_id="sales1", role="sales")  # MANAGE_LEADS
    await client.post("/api/leads", json={"email": "a@b.com", "clinic_name": "A"})

    lst = await client.get("/api/admin/leads", headers=_h("sales1"))
    assert lst.status_code == 200 and len(lst.json()) == 1
    lead_id = lst.json()[0]["id"]

    patch = await client.patch(f"/api/admin/leads/{lead_id}", headers=_h("sales1"),
                               json={"status": "contacted", "notes": "called back"})
    assert patch.status_code == 200
    assert patch.json()["status"] == "contacted" and patch.json()["notes"] == "called back"

    # status filter works
    assert len((await client.get(
        "/api/admin/leads", params={"status": "new"}, headers=_h("sales1")
    )).json()) == 0
    assert len((await client.get(
        "/api/admin/leads", params={"status": "contacted"}, headers=_h("sales1")
    )).json()) == 1


async def test_admin_lead_validation_and_404(client, db_session):
    await _internal(db_session, clerk_id="sales2", role="sales")
    await client.post("/api/leads", json={"email": "c@d.com"})
    lead_id = (await client.get("/api/admin/leads", headers=_h("sales2"))).json()[0]["id"]

    bad = await client.patch(f"/api/admin/leads/{lead_id}", headers=_h("sales2"),
                             json={"status": "bogus"})
    assert bad.status_code == 422
    missing = await client.patch(f"/api/admin/leads/{uuid.uuid4()}", headers=_h("sales2"),
                                 json={"status": "won"})
    assert missing.status_code == 404


async def test_leads_admin_denied_to_clinic_user(client, db_session):
    # A clinic user (not internal Dentiva staff) can't reach the admin inbox.
    r = await client.get("/api/admin/leads", headers=_h("random_clinic_user"))
    assert r.status_code in (401, 403)
