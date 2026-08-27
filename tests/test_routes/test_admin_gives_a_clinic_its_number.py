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


async def test_a_wrong_number_can_be_taken_off(client, db_session):
    """ai_phone_number was write-once: set it, and provisioning returned it
    unchanged forever. The first real clinic showed the cost — their OWN practice
    line was attached as the Dentovox number, so nothing could route there, and
    had they forwarded to it the line would have called itself. The guard is
    right about the danger; it just had no correction path."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Detach Co", clerk_org_id="org_det", clerk_user_id="u_det"
    )
    practice.ai_phone_number = "+19782837200"
    await db_session.commit()

    r = await client.request(
        "DELETE", f"/api/admin/clinics/{practice.id}/number",
        headers=_h("sa_num"), json={"confirm_number": "(978) 283-7200"},
    )
    assert r.status_code == 200
    await db_session.refresh(practice)
    assert practice.ai_phone_number is None


async def test_detaching_needs_the_number_typed_out(client, db_session):
    """Not a checkbox. The usual reason a number is wrong is that somebody
    clicked through once already."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Confirm Co", clerk_org_id="org_cnf", clerk_user_id="u_cnf"
    )
    practice.ai_phone_number = "+16175550100"
    await db_session.commit()

    r = await client.request(
        "DELETE", f"/api/admin/clinics/{practice.id}/number",
        headers=_h("sa_num"), json={"confirm_number": "+16175559999"},
    )
    assert r.status_code == 422
    await db_session.refresh(practice)
    assert practice.ai_phone_number == "+16175550100"


async def test_detaching_frees_the_row_for_the_right_number(client, db_session):
    """The whole point of the correction."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Redo Co", clerk_org_id="org_redo", clerk_user_id="u_redo"
    )
    practice.ai_phone_number = "+19782837200"
    await db_session.commit()

    await client.request(
        "DELETE", f"/api/admin/clinics/{practice.id}/number",
        headers=_h("sa_num"), json={"confirm_number": "+19782837200"},
    )
    r = await client.post(
        f"/api/admin/clinics/{practice.id}/provision-number",
        headers=_h("sa_num"), json={"number": "+19785550143"},
    )
    assert r.status_code == 200
    assert r.json()["ai_phone_number"] == "+19785550143"


async def test_an_operator_can_fill_knowledge_and_hours(client, db_session):
    """Both lived only behind the clinic's own login. That is right for a
    practice that signs itself up, and impossible for the two ways we actually
    onboard: a busy dentist who sends their insurances in a message, and a group
    whose 200 locations will never each open a wizard."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Fill Co", clerk_org_id="org_fill", clerk_user_id="u_fill"
    )

    r = await client.put(
        f"/api/admin/clinics/{practice.id}/profile-fill",
        headers=_h("sa_num"),
        json={
            "knowledge_base": {
                "providers": [{"name": "Dr. Sergey Zemlyansky", "type": "general"}],
                "insurances": ["Delta Dental", "MassHealth"],
                "appointment_types": [{"name": "Cleaning", "minutes": 45,
                                       "provider_type": "hygienist"}],
            },
            "business_hours": {
                "mon": {"open": "09:00", "close": "18:00"},
                "tue": {"open": "09:00", "close": "18:00"},
                "wed": None, "thu": {"open": "09:00", "close": "16:30"},
                "fri": {"open": "09:00", "close": "16:30"},
                "sat": None, "sun": None,
            },
        },
    )
    assert r.status_code == 200

    await db_session.refresh(practice)
    assert practice.knowledge_base["insurances"] == ["Delta Dental", "MassHealth"]
    assert practice.knowledge_base["providers"][0]["name"] == "Dr. Sergey Zemlyansky"
    assert practice.business_hours["thu"] == {"open": "09:00", "close": "16:30"}
    assert practice.business_hours["wed"] is None


async def test_the_operator_path_validates_exactly_like_the_clinics_own(
    client, db_session
):
    """Reusing the clinic-facing schemas is the point: a second, looser validator
    is how two paths to one column start disagreeing, and the disagreement shows
    up as an agent saying something the clinic never configured."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Strict Co", clerk_org_id="org_strict", clerk_user_id="u_strict"
    )

    r = await client.put(
        f"/api/admin/clinics/{practice.id}/profile-fill",
        headers=_h("sa_num"),
        # 25:00 is not a time; the clinic's own endpoint refuses it too.
        json={"business_hours": {"mon": {"open": "09:00", "close": "25:00"},
                                 "tue": None, "wed": None, "thu": None,
                                 "fri": None, "sat": None, "sun": None}},
    )
    assert r.status_code in (400, 422)
    await db_session.refresh(practice)
    assert practice.business_hours.get("mon") != {"open": "09:00", "close": "25:00"}


async def test_filling_nothing_is_refused_rather_than_silently_accepted(
    client, db_session
):
    """An empty PUT that returns 200 reads as "saved" to whoever sent it."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Empty Co", clerk_org_id="org_empty", clerk_user_id="u_empty"
    )
    r = await client.put(
        f"/api/admin/clinics/{practice.id}/profile-fill",
        headers=_h("sa_num"), json={},
    )
    assert r.status_code == 422
