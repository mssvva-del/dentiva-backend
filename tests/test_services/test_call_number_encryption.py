"""B3 — call phone numbers encrypted at rest + searchable via caller_number_hmac."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.db import set_tenant
from app.models.call import Call
from app.utils.crypto import phone_hmac
from tests.conftest import seed_practice


def _call(practice_id, *, from_number, to_number="+16204559562", recording=None) -> Call:
    return Call(
        id=uuid.uuid4(), practice_id=practice_id, direction="inbound",
        from_number=from_number, to_number=to_number,
        started_at=datetime.now(tz=UTC), status="completed", recording_path=recording,
    )


async def test_numbers_encrypted_at_rest_hmac_set(db_session):
    practice, _ = await seed_practice(
        db_session, name="CN1", clerk_org_id="o_cn1", clerk_user_id="u_cn1"
    )
    await set_tenant(db_session, practice.id)
    c = _call(practice.id, from_number="+13055550199", recording="s3://rec/a.wav")
    db_session.add(c)
    await db_session.commit()

    got = (await db_session.execute(select(Call).where(Call.id == c.id))).scalar_one()
    assert got.from_number == "+13055550199"          # ORM decrypts transparently
    assert got.recording_path == "s3://rec/a.wav"
    assert got.caller_number_hmac == phone_hmac("+13055550199")

    raw = (await db_session.execute(
        text("SELECT from_number, recording_path FROM calls WHERE id = :id"), {"id": c.id}
    )).one()
    assert bytes(raw[0]).startswith(b"gAAAA")          # Fernet ciphertext
    assert b"3055550199" not in bytes(raw[0])
    assert b"rec/a.wav" not in bytes(raw[1])


async def test_call_search_by_number_uses_hmac(client, db_session):
    org, user = "o_cn2", "u_cn2"
    practice, _ = await seed_practice(
        db_session, name="CN2", clerk_org_id=org, clerk_user_id=user
    )
    await set_tenant(db_session, practice.id)
    db_session.add(_call(practice.id, from_number="+13051112222"))
    db_session.add(_call(practice.id, from_number="+13059998888"))
    await db_session.commit()

    hdr = {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}
    # Search by the full number (any format) → exact hmac match, one result.
    # Phone search goes in the POST body (not the URL) — PHI out of logs.
    r = await client.post("/api/calls/search", json={"search": "(305) 111-2222"}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["calls"][0]["from_number"] == "+13051112222"

    # A number not present → zero.
    r0 = await client.post("/api/calls/search", json={"search": "3050000000"}, headers=hdr)
    assert r0.json()["total"] == 0
    # No search → both calls returned.
    rall = await client.get("/api/calls", headers=hdr)
    assert rall.json()["total"] == 2
