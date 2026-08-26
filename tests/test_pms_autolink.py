"""The last inch of PMS setup, which used to need a human.

The clinic's setup screen promises it will say "connected" on its own. These
tests hold that promise to the one thing that makes it safe: an ambiguous match
links nothing, because linking the wrong location means reading one clinic's
calendar aloud to another clinic's patient.
"""

import pytest
from sqlalchemy import update

from app.models.practice import Practice
from app.services import maintenance
from tests.conftest import seed_practice

pytestmark = pytest.mark.asyncio


async def _only_these_practices_have_pms(db_session):
    """This function reasons over EVERY practice in the database, so a practice
    another test left behind with an installer key changes its answer. Clear the
    field first: the tests then say what they mean regardless of run order."""
    await db_session.execute(update(Practice).values(pms_credentials=None))
    await db_session.commit()


async def _waiting(db_session, *, name, org, key="PK-123"):
    p, _ = await seed_practice(
        db_session, name=name, clerk_org_id=org, clerk_user_id=f"u_{org}"
    )
    p.pms_credentials = {"bridge": "nexhealth", "product_key": key}
    await db_session.commit()
    return p


async def test_links_the_unambiguous_case(db_session, monkeypatch):
    """One clinic waiting, one location appears → connected, no human involved."""
    await _only_these_practices_have_pms(db_session)
    p = await _waiting(db_session, name="Bright Smiles", org="org_link1")

    class FakeClient:
        async def list_locations(self):
            return [{"id": "777", "name": "Bright Smiles"}]

    monkeypatch.setattr(
        "app.adapters.nexhealth.client.NexHealthClient", lambda *a, **k: FakeClient()
    )
    monkeypatch.setattr(maintenance.get_settings(), "nexhealth_api_key", "test-key")

    assert await maintenance.link_synced_locations() == 1
    await db_session.refresh(p)
    assert p.pms_credentials["location_id"] == "777"
    # The installer key survives — the clinic still reads it off its own screen.
    assert p.pms_credentials["product_key"] == "PK-123"


async def test_two_waiting_clinics_are_matched_by_name(db_session, monkeypatch):
    """This assertion used to run the other way.

    The original rule was "exactly one waiting, exactly one unclaimed" — refuse
    otherwise. Safe, and useless the moment a group practice rolls out: with a
    fleet installing over the same fortnight it never fires once.

    Names decide now, and only when unambiguous on both sides. Here two clinics
    wait, "Bright Smiles" and "Bright Smile Dental"; one location arrives named
    "Bright Smiles". It belongs to exactly one of them, and the other keeps
    waiting for its own — which is the answer a human would have given.

    Identical names are still deferred; that case has its own test in
    tests/test_scale/."""
    await _only_these_practices_have_pms(db_session)
    a = await _waiting(db_session, name="Bright Smiles", org="org_amb1", key="PK-A")
    b = await _waiting(db_session, name="Bright Smile Dental", org="org_amb2", key="PK-B")

    class FakeClient:
        async def list_locations(self):
            return [{"id": "801", "name": "Bright Smiles"}]

    monkeypatch.setattr(
        "app.adapters.nexhealth.client.NexHealthClient", lambda *a, **k: FakeClient()
    )
    monkeypatch.setattr(maintenance.get_settings(), "nexhealth_api_key", "test-key")

    assert await maintenance.link_synced_locations() == 1
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.pms_credentials["location_id"] == "801"
    assert "location_id" not in (b.pms_credentials or {}), (
        "the clinic whose location has not arrived must keep waiting"
    )


async def test_already_claimed_location_is_never_stolen(
    db_session, monkeypatch
):
    """A location another practice already uses is not a candidate — otherwise the
    second clinic to install would be handed the first clinic's calendar."""
    await _only_these_practices_have_pms(db_session)
    taken, _ = await seed_practice(
        db_session, name="Taken", clerk_org_id="org_taken", clerk_user_id="u_taken"
    )
    taken.pms_credentials = {"bridge": "nexhealth", "location_id": "900"}
    waiting = await _waiting(db_session, name="Waiting", org="org_wait1")
    await db_session.commit()

    class FakeClient:
        async def list_locations(self):
            return [{"id": "900", "name": "Taken"}]

    monkeypatch.setattr(
        "app.adapters.nexhealth.client.NexHealthClient", lambda *a, **k: FakeClient()
    )
    monkeypatch.setattr(maintenance.get_settings(), "nexhealth_api_key", "test-key")

    assert await maintenance.link_synced_locations() == 0
    await db_session.refresh(waiting)
    assert "location_id" not in (waiting.pms_credentials or {})
