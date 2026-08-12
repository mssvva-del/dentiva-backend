"""Creating a clinic from the admin panel, without leaving an orphan behind.

Identity lives in Clerk. Both paths that create a practice key on a Clerk
organization, so a clinic created here without one cannot be signed in to — and
when the owner eventually creates their own organization, a SECOND practice
appears beside it with the calls and the billing on the wrong one.

Which makes the failure case the interesting one, not the happy path.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.practice import Practice
from tests.test_routes.test_admin import _h, _internal


async def _staff(db_session):
    await _internal(db_session, clerk_id="sa_create", role="super_admin")


async def test_a_clinic_is_created_with_its_organization(client, db_session, monkeypatch):
    from app.routes import admin as admin_routes  # noqa: F401
    from app.services import clerk_api

    await _staff(db_session)

    async def _org(*, name):
        return "org_created_123"

    monkeypatch.setattr(clerk_api, "create_organization", _org)

    r = await client.post(
        "/api/admin/clinics",
        headers=_h("sa_create"),
        json={"name": "Bright Smiles", "timezone": "America/Chicago"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Bright Smiles"
    assert body["status"] == "onboarding"
    assert body["onboarding_step"] == 1, "it should land where a self-signup would"

    practice = (await db_session.execute(
        select(Practice).where(Practice.clerk_org_id == "org_created_123")
    )).scalar_one()
    assert practice.timezone == "America/Chicago"
    assert practice.pms_system == "none"


async def test_no_organization_means_no_clinic_row(client, db_session, monkeypatch):
    """THE test. A half-created clinic is worse than no button: the row looks
    real in every list while being unreachable, and the person who finds out is
    the customer trying to log in."""
    from app.services import clerk_api

    await _staff(db_session)
    before = (await db_session.execute(
        select(func.count()).select_from(Practice)
    )).scalar_one()

    async def _no_org(*, name):
        return None

    monkeypatch.setattr(clerk_api, "create_organization", _no_org)

    r = await client.post(
        "/api/admin/clinics", headers=_h("sa_create"), json={"name": "Ghost Dental"},
    )
    assert r.status_code == 503
    # The app wraps errors as {"error": {code, message, details}} — the operator
    # has to be told WHY nothing was created, or they will retry and eventually
    # succeed at making the orphan by hand.
    assert "cannot be signed in to" in r.json()["error"]["message"]

    after = (await db_session.execute(
        select(func.count()).select_from(Practice)
    )).scalar_one()
    assert after == before, "a clinic nobody can reach was left in the database"


async def test_an_owner_invitation_failing_does_not_undo_the_clinic(
    client, db_session, monkeypatch
):
    """The invitation is sent after the commit on purpose. Failing the request
    over a bounced email would leave the operator retrying a create that then
    makes a second organization."""
    from app.services import clerk_api

    await _staff(db_session)

    async def _org(*, name):
        return "org_invite_fail"

    async def _bad_invite(**kwargs):
        return None

    monkeypatch.setattr(clerk_api, "create_organization", _org)
    monkeypatch.setattr(clerk_api, "create_org_invitation", _bad_invite)

    r = await client.post(
        "/api/admin/clinics", headers=_h("sa_create"),
        json={"name": "Invite Co", "owner_email": "owner@example.com"},
    )
    assert r.status_code == 201
    assert (await db_session.execute(
        select(Practice).where(Practice.clerk_org_id == "org_invite_fail")
    )).scalar_one_or_none() is not None


async def test_a_nameless_clinic_is_refused(client, db_session, monkeypatch):
    from app.services import clerk_api

    await _staff(db_session)
    called: list[str] = []

    async def _org(*, name):
        called.append(name)
        return "org_x"

    monkeypatch.setattr(clerk_api, "create_organization", _org)
    r = await client.post(
        "/api/admin/clinics", headers=_h("sa_create"), json={"name": "   "},
    )
    assert r.status_code == 422
    assert called == [], "an organization was created for a clinic we then refused"


async def test_a_clinic_user_cannot_create_clinics(client, db_session, monkeypatch):
    from app.services import clerk_api
    from tests.conftest import seed_practice

    await seed_practice(
        db_session, name="Outsider", clerk_org_id="org_out", clerk_user_id="outsider"
    )

    async def _org(*, name):
        raise AssertionError("reached Clerk without permission")

    monkeypatch.setattr(clerk_api, "create_organization", _org)
    r = await client.post(
        "/api/admin/clinics",
        headers={"X-Dev-Clerk-User-Id": "outsider", "X-Dev-Clerk-Org-Id": "org_out"},
        json={"name": "Sneaky Dental"},
    )
    assert r.status_code in (401, 403)
