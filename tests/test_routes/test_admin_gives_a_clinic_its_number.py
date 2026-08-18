"""The operator's path to a phone number, which did not exist.

ai_phone_number had exactly two writers: the onboarding wizard and the canary
bootstrap. When provisioning failed mid-onboarding, the clinic finished setup
with no number, the wizard never returns to that step, and the only repair was
SQL against production. Routing keys off this column — a clinic without it is a
clinic whose calls reach nobody.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.practice import Practice
from tests.conftest import seed_practice
from tests.test_routes.test_admin import _h, _internal


async def _staff(db_session):
    await _internal(db_session, clerk_id="sa_num", role="super_admin")


async def test_an_operator_can_attach_a_number_bought_by_hand(client, db_session):
    """The repair path: Retell was down during onboarding, somebody bought the
    number in their dashboard, and it has to reach the practice row."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Numberless", clerk_org_id="org_num1", clerk_user_id="u_num1"
    )
    practice_id = practice.id

    r = await client.post(
        f"/api/admin/clinics/{practice_id}/provision-number",
        headers=_h("sa_num"), json={"number": "(620) 555-0100"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ai_phone_number"] == "+16205550100"

    db_session.expire_all()
    stored = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    assert stored.ai_phone_number == "+16205550100"


async def test_a_clinic_with_a_number_keeps_it(client, db_session):
    """Idempotent, same as onboarding: clicking twice must never change a number
    calls already route on."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Has One", clerk_org_id="org_num2", clerk_user_id="u_num2"
    )
    practice.ai_phone_number = "+16205550111"
    await db_session.commit()

    r = await client.post(
        f"/api/admin/clinics/{practice.id}/provision-number",
        headers=_h("sa_num"), json={"number": "+16205550999"},
    )
    assert r.status_code == 200
    assert r.json()["ai_phone_number"] == "+16205550111", (
        "an operator overwrote a number live calls were routing on"
    )


async def test_a_number_cannot_be_attached_to_two_clinics(client, db_session):
    """THE test. Routing keys off this column; two clinics answering one number
    is the cross-tenant failure everything else here guards against."""
    await _staff(db_session)
    first, _ = await seed_practice(
        db_session, name="First Owner", clerk_org_id="org_num3", clerk_user_id="u_num3"
    )
    first.ai_phone_number = "+16205550122"
    await db_session.commit()
    second, _ = await seed_practice(
        db_session, name="Second", clerk_org_id="org_num4", clerk_user_id="u_num4"
    )

    r = await client.post(
        f"/api/admin/clinics/{second.id}/provision-number",
        headers=_h("sa_num"), json={"number": "+16205550122"},
    )
    assert r.status_code == 409
    # Says WHOSE it is, so the operator fixes the right row instead of hunting.
    assert "First Owner" in r.json()["error"]["message"]


async def test_garbage_is_refused_not_normalized_into_something(client, db_session):
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Typo Dental", clerk_org_id="org_num5", clerk_user_id="u_num5"
    )
    r = await client.post(
        f"/api/admin/clinics/{practice.id}/provision-number",
        headers=_h("sa_num"), json={"number": "12345"},
    )
    assert r.status_code == 422


async def test_buying_for_an_unapproved_clinic_is_refused_with_the_reason(
    client, db_session
):
    """Empty body means buy from Retell, and an onboarding practice is not
    entitled — the refusal has to say what to do, not just no."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Still Onboarding", clerk_org_id="org_num6", clerk_user_id="u_num6"
    )
    practice.status = "onboarding"
    await db_session.commit()

    r = await client.post(
        f"/api/admin/clinics/{practice.id}/provision-number",
        headers=_h("sa_num"), json={},
    )
    assert r.status_code == 409
    assert "Approve the clinic" in r.json()["error"]["message"]


async def test_a_clinic_user_cannot_give_numbers(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Outsider3", clerk_org_id="org_num7", clerk_user_id="u_num7"
    )
    r = await client.post(
        f"/api/admin/clinics/{practice.id}/provision-number",
        headers={"X-Dev-Clerk-User-Id": "u_num7", "X-Dev-Clerk-Org-Id": "org_num7"},
        json={"number": "+16205550133"},
    )
    assert r.status_code in (401, 403)
