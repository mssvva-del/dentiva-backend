"""Giving a clinic its own PMS bridge, from the admin panel.

The deployment's NEXHEALTH_*/KOLLA_* describe ONE location. Binding them to a
named practice stopped clinic number two reading clinic number one's calendar,
but left it with no PMS at all — and the only way to give it one was a redeploy,
while a practice waited.

Two things matter here and neither is the happy path: that a half-filled set is
refused rather than stored (it would fail mid-call, not at save time), and that
the key never comes back out of the API.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.practice import Practice
from tests.conftest import seed_practice
from tests.test_routes.test_admin import _h, _internal

_NEXHEALTH = {
    "bridge": "nexhealth",
    "api_key": "nx_live_secret",
    "subdomain": "brightsmiles",
    "location_id": "351939",
}


async def _staff(db_session):
    await _internal(db_session, clerk_id="sa_pms", role="super_admin")


async def test_a_clinic_gets_its_own_bridge(client, db_session):
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Second Clinic", clerk_org_id="org_pms2", clerk_user_id="u_pms2"
    )

    r = await client.put(
        f"/api/admin/clinics/{practice.id}/pms-credentials",
        headers=_h("sa_pms"),
        json=_NEXHEALTH,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "practice_id": str(practice.id), "bridge": "nexhealth", "configured": True,
    }

    practice_id = practice.id
    db_session.expire_all()
    stored = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    assert stored.pms_credentials["location_id"] == "351939"


async def test_half_filled_credentials_are_refused_rather_than_stored(client, db_session):
    """THE test. A NexHealth client missing a location id builds fine and fails
    in the middle of a call — the patient hears "let me check" and then nothing.
    Validation has to run through the same function the voice path uses."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Partial", clerk_org_id="org_pms3", clerk_user_id="u_pms3"
    )

    r = await client.put(
        f"/api/admin/clinics/{practice.id}/pms-credentials",
        headers=_h("sa_pms"),
        json={"bridge": "nexhealth", "api_key": "k", "subdomain": "s"},
    )
    assert r.status_code == 422
    practice_id = practice.id
    db_session.expire_all()
    stored = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    assert stored.pms_credentials is None, "an unusable bridge was saved anyway"


async def test_the_key_never_comes_back_out(client, db_session):
    """A live key into a dental practice's own system is a larger thing to leak
    than any one patient record. The detail card may say which bridge answers;
    it may not say with what."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Quiet", clerk_org_id="org_pms4", clerk_user_id="u_pms4"
    )
    await client.put(
        f"/api/admin/clinics/{practice.id}/pms-credentials",
        headers=_h("sa_pms"), json=_NEXHEALTH,
    )

    r = await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sa_pms"))
    assert r.status_code == 200, r.text
    assert "nx_live_secret" not in r.text
    assert r.json()["pms_credentials_own"] is True


async def test_a_clinic_user_cannot_set_another_clinics_bridge(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Outsider2", clerk_org_id="org_pms5", clerk_user_id="u_pms5"
    )
    r = await client.put(
        f"/api/admin/clinics/{practice.id}/pms-credentials",
        headers={"X-Dev-Clerk-User-Id": "u_pms5", "X-Dev-Clerk-Org-Id": "org_pms5"},
        json=_NEXHEALTH,
    )
    assert r.status_code in (401, 403)
