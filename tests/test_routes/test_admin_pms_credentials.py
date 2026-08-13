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


# ── the gap between "we started it" and "their calendar answers" ────────────
#
# Creating the institution and the practice finishing the install are hours or
# days apart. The clinic needs the installer key during that gap, and used to get
# it in a forwarded email — then had to ask us whether it had worked.


async def test_an_installer_key_can_be_stored_before_any_location_exists(
    client, db_session
):
    """A location cannot exist until the practice has run the installer, and the
    installer cannot be run without this key. Refusing the key for want of a
    location would deadlock the only path there is."""
    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Waiting", clerk_org_id="org_pms6", clerk_user_id="u_pms6"
    )

    r = await client.put(
        f"/api/admin/clinics/{practice.id}/pms-credentials",
        headers=_h("sa_pms"),
        json={"bridge": "nexhealth", "product_key": "PK-TEST-1234"},
    )
    assert r.status_code == 200, r.text
    # Honest: stored, and not yet connected. The clinic's screen says "waiting".
    assert r.json()["configured"] is False

    practice_id = practice.id
    db_session.expire_all()
    stored = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    assert stored.pms_credentials["product_key"] == "PK-TEST-1234"


async def test_linking_the_location_later_keeps_the_installer_key(
    client, db_session, monkeypatch
):
    """THE test. The second call carries only the location — and used to replace
    the whole record, wiping the key the clinic was still reading off its own
    screen while the install was in progress."""
    from app.adapters import bridge

    # The account key lives in the environment, and the suite blanks real
    # credentials — so a location alone would be refused here for the right
    # reason (a location with no key addresses nothing) and hide the wrong one.
    monkeypatch.setattr(bridge, "get_settings", lambda: type("S", (), {
        "nexhealth_api_key": "account-key", "kolla_api_key": "",
        "kolla_consumer_id": "", "kolla_connector_id": "",
        "nexhealth_subdomain": "acct", "nexhealth_location_id": "",
        "pms_env_practice_id": "",
    })())

    await _staff(db_session)
    practice, _ = await seed_practice(
        db_session, name="Two Step", clerk_org_id="org_pms7", clerk_user_id="u_pms7"
    )
    for body in (
        {"bridge": "nexhealth", "product_key": "PK-TEST-9999"},
        {"bridge": "nexhealth", "location_id": "351939"},
    ):
        r = await client.put(
            f"/api/admin/clinics/{practice.id}/pms-credentials",
            headers=_h("sa_pms"), json=body,
        )
        assert r.status_code == 200, r.text

    practice_id = practice.id
    db_session.expire_all()
    stored = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    assert stored.pms_credentials["location_id"] == "351939"
    assert stored.pms_credentials["product_key"] == "PK-TEST-9999", (
        "linking the calendar erased the key the clinic was still using"
    )
