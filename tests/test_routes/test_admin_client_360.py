"""ADM-CLIENT-360 — rich clinic profile + BAA history + admin edit."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.baa_acceptance import BaaAcceptance
from app.models.dentiva_staff import DentivaStaff
from app.models.practice import Practice
from app.models.user import User
from tests.conftest import seed_practice


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


async def test_clinic_detail_full_profile(client, db_session):
    await _internal(db_session, clerk_id="sa_360", role="super_admin")
    practice, owner = await seed_practice(db_session, name="Profile Dental",
                                          clerk_org_id="o_360", clerk_user_id="owner_360")
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id))).scalar_one()
    p.address = "10 Ocean Ave"
    p.agent_settings = {"agent_name": "Sofia", "greeting": "Welcome!"}
    p.knowledge_base = {"providers": [{"name": "Dr. Chen"}],
                        "insurances": ["Delta", "Cigna"],
                        "policies": {"cancellation": "24h"}}
    await db_session.commit()

    r = await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sa_360"))
    assert r.status_code == 200
    b = r.json()
    assert b["address"] == "10 Ocean Ave"
    assert b["agent_name"] == "Sofia" and b["agent_greeting"] == "Welcome!"
    assert b["kb_providers"] == 1 and b["kb_insurances"] == 2
    assert b["kb_has_policies"] is True
    assert b["owner_email"] == "owner_360@example.com"
    assert "forwarding_instruction" in b and "business_hours" in b


async def test_baa_history(client, db_session):
    await _internal(db_session, clerk_id="sa_baa", role="super_admin")
    practice, _ = await seed_practice(db_session, name="BAA Co",
                                      clerk_org_id="o_baa", clerk_user_id="u_baa")
    db_session.add(BaaAcceptance(
        id=uuid.uuid4(), practice_id=practice.id, document_version="2026-07-draft-1",
        signer_name="Dr. Ruiz", signer_title="Owner", signer_ip="1.2.3.4"))
    await db_session.commit()

    r = await client.get(f"/api/admin/clinics/{practice.id}/baa-history",
                         headers=_h("sa_baa"))
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["signer_name"] == "Dr. Ruiz"
    assert rows[0]["document_version"] == "2026-07-draft-1"


async def test_admin_edit_clinic(client, db_session):
    await _internal(db_session, clerk_id="sa_edit", role="super_admin")
    practice, _ = await seed_practice(db_session, name="Old Name",
                                      clerk_org_id="o_edit", clerk_user_id="u_edit")

    r = await client.patch(
        f"/api/admin/clinics/{practice.id}", headers=_h("sa_edit"),
        json={"name": "New Name", "timezone": "America/Chicago",
              "agent_name": "Maya", "phone_number": "+13055550100"},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["name"] == "New Name" and b["timezone"] == "America/Chicago"
    assert b["agent_name"] == "Maya"
    assert b["phone_number"] == "+13055550100"

    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id))).scalar_one()
    await db_session.refresh(p)
    assert p.name == "New Name"
    assert p.agent_settings["agent_name"] == "Maya"


async def test_admin_edit_rbac(client, db_session):
    # finance lacks MANAGE_CLINIC_STATUS → 403; a random clinic user → 401/403.
    await _internal(db_session, clerk_id="fin_edit", role="finance")
    practice, _ = await seed_practice(db_session, name="RB Co",
                                      clerk_org_id="o_rb", clerk_user_id="u_rb")
    r = await client.patch(f"/api/admin/clinics/{practice.id}", headers=_h("fin_edit"),
                           json={"name": "X"})
    assert r.status_code in (401, 403)
