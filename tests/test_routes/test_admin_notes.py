"""ADM10 — clinic notes: create/list/delete + author-or-super_admin delete guard."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.clinic_note import ClinicNote
from app.models.dentiva_staff import DentivaStaff
from app.models.user import User
from tests.conftest import seed_practice


async def _internal(db_session, *, clerk_id, role, email=None):
    u = User(id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
             email=email or f"{clerk_id}@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()
    return u


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": "org_internal"}


async def test_add_list_note(client, db_session):
    await _internal(db_session, clerk_id="sup_n1", role="support",
                    email="sam@dentovox.com")
    practice, _ = await seed_practice(db_session, name="NoteCo",
                                      clerk_org_id="o_n1", clerk_user_id="u_n1")
    r = await client.post(f"/api/admin/clinics/{practice.id}/notes",
                          headers=_h("sup_n1"), json={"body": "  Called owner, will pilot  "})
    assert r.status_code == 200
    assert r.json()["body"] == "Called owner, will pilot"     # trimmed
    assert r.json()["author_email"] == "sam@dentovox.com"

    lst = await client.get(f"/api/admin/clinics/{practice.id}/notes", headers=_h("sup_n1"))
    assert lst.status_code == 200 and len(lst.json()) == 1


async def test_add_note_validation_and_missing_clinic(client, db_session):
    await _internal(db_session, clerk_id="sup_n2", role="support")
    practice, _ = await seed_practice(db_session, name="NoteCo2",
                                      clerk_org_id="o_n2", clerk_user_id="u_n2")
    empty = await client.post(f"/api/admin/clinics/{practice.id}/notes",
                              headers=_h("sup_n2"), json={"body": "   "})
    assert empty.status_code == 422
    toolong = await client.post(f"/api/admin/clinics/{practice.id}/notes",
                                headers=_h("sup_n2"), json={"body": "x" * 5001})
    assert toolong.status_code == 422
    missing = await client.post(f"/api/admin/clinics/{uuid.uuid4()}/notes",
                                headers=_h("sup_n2"), json={"body": "hi"})
    assert missing.status_code == 404


async def test_delete_author_only_or_super_admin(client, db_session):
    author = await _internal(db_session, clerk_id="sup_a", role="support")
    await _internal(db_session, clerk_id="sup_b", role="support")   # different staffer
    await _internal(db_session, clerk_id="sa_n", role="super_admin")
    practice, _ = await seed_practice(db_session, name="NoteCo3",
                                      clerk_org_id="o_n3", clerk_user_id="u_n3")
    made = await client.post(f"/api/admin/clinics/{practice.id}/notes",
                             headers=_h("sup_a"), json={"body": "author note"})
    note_id = made.json()["id"]

    # another support staffer can't delete someone else's note
    other = await client.delete(
        f"/api/admin/clinics/{practice.id}/notes/{note_id}", headers=_h("sup_b"))
    assert other.status_code == 403
    # super_admin can
    sa = await client.delete(
        f"/api/admin/clinics/{practice.id}/notes/{note_id}", headers=_h("sa_n"))
    assert sa.status_code == 200
    gone = (await db_session.execute(
        select(ClinicNote).where(ClinicNote.id == uuid.UUID(note_id))
    )).scalar_one_or_none()
    assert gone is None
    assert author is not None

    # deleting a missing note → 404
    r404 = await client.delete(
        f"/api/admin/clinics/{practice.id}/notes/{uuid.uuid4()}", headers=_h("sa_n"))
    assert r404.status_code == 404


async def test_delete_by_author_allowed(client, db_session):
    await _internal(db_session, clerk_id="sup_c", role="support")
    practice, _ = await seed_practice(db_session, name="NoteCo4",
                                      clerk_org_id="o_n4", clerk_user_id="u_n4")
    made = await client.post(f"/api/admin/clinics/{practice.id}/notes",
                             headers=_h("sup_c"), json={"body": "mine"})
    r = await client.delete(
        f"/api/admin/clinics/{practice.id}/notes/{made.json()['id']}", headers=_h("sup_c"))
    assert r.status_code == 200


async def test_notes_denied_without_clinic_detail(client, db_session):
    # engineer role lacks VIEW_CLINIC_DETAIL.
    await _internal(db_session, clerk_id="eng_n", role="engineer")
    practice, _ = await seed_practice(db_session, name="NoteCo5",
                                      clerk_org_id="o_n5", clerk_user_id="u_n5")
    for who in ("eng_n", "random_clinic"):
        r = await client.get(f"/api/admin/clinics/{practice.id}/notes", headers=_h(who))
        assert r.status_code in (401, 403), who
