"""A recording link that outlives the reason to have it.

The dashboard served whatever URL arrived on the webhook, stored and handed back
months later. That works only while the link is permanent — which is to say,
while a recording of a patient describing their symptoms is readable by anyone
who ever saw the URL, with no login, forever.

Retell can sign these links so they expire. Enabling that would have broken every
stored one: the play button would have led nowhere, silently, for every call
older than a day. Fetching at the moment somebody presses play is what makes the
setting safe to turn on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.call import Call
from tests.conftest import seed_practice


async def _call_row(db_session, practice_id, *, stored: str | None, retell_id: str | None):
    call = Call(
        id=uuid.uuid4(), practice_id=practice_id, retell_call_id=retell_id,
        direction="inbound", from_number="+16205551111", to_number="+15559876543",
        started_at=datetime.now(UTC), status="completed", duration_seconds=90,
        recording_path=stored,
    )
    db_session.add(call)
    await db_session.commit()
    return call.id


async def test_the_link_is_fetched_fresh_not_served_from_storage(
    client, db_session, monkeypatch
):
    """THE test. The stored link is stale by construction once signing is on."""
    from app.routes import calls as calls_route

    practice, _ = await seed_practice(
        db_session, name="Rec Dental", clerk_org_id="org_rec1", clerk_user_id="u_rec1"
    )
    call_id = await _call_row(
        db_session, practice.id, stored="https://old.example/stale.wav", retell_id="rc_1"
    )

    async def _fresh(retell_call_id, **kwargs):
        assert retell_call_id == "rc_1"
        return "https://retell.example/signed?exp=soon"

    monkeypatch.setattr(calls_route, "recording_url_for_call", _fresh, raising=False)
    import app.services.retell_admin as admin
    monkeypatch.setattr(admin, "recording_url_for_call", _fresh)

    r = await client.get(
        f"/api/calls/{call_id}",
        headers={"X-Dev-Clerk-User-Id": "u_rec1", "X-Dev-Clerk-Org-Id": "org_rec1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["recording_url"] == "https://retell.example/signed?exp=soon"


async def test_retell_being_unreachable_falls_back_rather_than_failing(
    client, db_session, monkeypatch
):
    """An old link beats no recording, and a call detail page must not 500
    because a vendor is down."""
    import app.services.retell_admin as admin

    practice, _ = await seed_practice(
        db_session, name="Rec Dental 2", clerk_org_id="org_rec2", clerk_user_id="u_rec2"
    )
    call_id = await _call_row(
        db_session, practice.id, stored="https://old.example/stale.wav", retell_id="rc_2"
    )

    async def _down(retell_call_id, **kwargs):
        return None

    monkeypatch.setattr(admin, "recording_url_for_call", _down)

    r = await client.get(
        f"/api/calls/{call_id}",
        headers={"X-Dev-Clerk-User-Id": "u_rec2", "X-Dev-Clerk-Org-Id": "org_rec2"},
    )
    assert r.status_code == 200
    assert r.json()["recording_url"] == "https://old.example/stale.wav"


async def test_a_call_with_no_recording_shows_none(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Rec Dental 3", clerk_org_id="org_rec3", clerk_user_id="u_rec3"
    )
    call_id = await _call_row(db_session, practice.id, stored=None, retell_id=None)

    r = await client.get(
        f"/api/calls/{call_id}",
        headers={"X-Dev-Clerk-User-Id": "u_rec3", "X-Dev-Clerk-Org-Id": "org_rec3"},
    )
    assert r.status_code == 200
    assert r.json()["recording_url"] is None
