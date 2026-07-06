"""Call PHI at rest — transcript encryption (B2) + retention scrub (B4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

import app.db as app_db
from app.db import set_tenant
from app.models.call import Call
from app.services.maintenance import scrub_expired_transcripts
from tests.conftest import seed_practice


def _call(practice_id, *, started_at, transcript=None, recording=None) -> Call:
    return Call(
        id=uuid.uuid4(), practice_id=practice_id, direction="inbound",
        from_number="+15551230000", to_number="+16204559562",
        started_at=started_at, status="completed",
        transcript_jsonb=transcript, recording_path=recording,
    )


async def test_transcript_encrypted_at_rest_but_readable_via_orm(db_session):
    practice, _ = await seed_practice(
        db_session, name="Enc1", clerk_org_id="o_enc1", clerk_user_id="u_enc1"
    )
    await set_tenant(db_session, practice.id)
    secret = [{"role": "user", "content": "My name is Bob Alvarez, 305-555-0199"}]
    c = _call(practice.id, started_at=datetime.now(tz=UTC), transcript=secret)
    db_session.add(c)
    await db_session.commit()

    # ORM read decrypts transparently → same object back.
    got = (await db_session.execute(select(Call).where(Call.id == c.id))).scalar_one()
    assert got.transcript_jsonb == secret

    # Raw column bytes must NOT contain the plaintext (it's Fernet ciphertext).
    raw = (await db_session.execute(
        text("SELECT transcript_jsonb FROM calls WHERE id = :id"), {"id": c.id}
    )).scalar_one()
    raw_bytes = bytes(raw)
    assert b"Alvarez" not in raw_bytes and b"305-555-0199" not in raw_bytes
    assert raw_bytes.startswith(b"gAAAA")  # Fernet token prefix


async def test_scrub_expired_transcripts_nulls_old_keeps_recent(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ret1", clerk_org_id="o_ret1", clerk_user_id="u_ret1"
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    await set_tenant(db_session, practice.id)
    old = _call(practice.id, started_at=now - timedelta(days=400),
                transcript=[{"content": "old PHI"}], recording="rec/old.wav")
    recent = _call(practice.id, started_at=now - timedelta(days=10),
                   transcript=[{"content": "recent PHI"}], recording="rec/new.wav")
    db_session.add_all([old, recent])
    await db_session.commit()

    scrubbed = await scrub_expired_transcripts(now=now, retention_days=365)
    assert scrubbed == 1

    async with app_db.async_session_factory() as s:
        await set_tenant(s, practice.id)
        rows = {r.id: r for r in (await s.execute(
            select(Call).where(Call.practice_id == practice.id)
        )).scalars().all()}
    assert rows[old.id].transcript_jsonb is None and rows[old.id].recording_path is None
    assert rows[recent.id].transcript_jsonb == [{"content": "recent PHI"}]
    assert rows[recent.id].recording_path == "rec/new.wav"


async def test_scrub_disabled_when_retention_zero(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ret0", clerk_org_id="o_ret0", clerk_user_id="u_ret0"
    )
    await set_tenant(db_session, practice.id)
    c = _call(practice.id, started_at=datetime(2020, 1, 1, tzinfo=UTC),
              transcript=[{"content": "x"}])
    db_session.add(c)
    await db_session.commit()
    assert await scrub_expired_transcripts(retention_days=0) == 0  # off → no-op
