"""GET /api/calls/{id} must mask PII in the returned transcript (H4 wiring)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.db import set_tenant
from app.models.call import Call
from app.models.patient import Patient
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def test_transcript_pii_is_redacted_in_response(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Redact Dental", clerk_org_id="org_red1", clerk_user_id="user_red1"
    )
    await set_tenant(db_session, practice.id)

    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="red-1",
        first_name="Maria", last_name="Lopez", phone="+15557770000",
    )
    db_session.add(patient)
    await db_session.flush()

    call = Call(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        retell_call_id="ret_red_1", direction="inbound",
        from_number="+15551112222", to_number="+15559998888",
        started_at=datetime.now(UTC), status="completed",
        transcript_jsonb=[
            {"role": "agent", "content": "Thanks for calling. Who's this?"},
            {
                "role": "user",
                "content": "This is Maria Lopez, reach me at 415-555-1234 or maria@x.com",
            },
        ],
    )
    db_session.add(call)
    await db_session.commit()

    resp = await client.get(
        f"/api/calls/{call.id}", headers=_auth("org_red1", "user_red1")
    )
    assert resp.status_code == 200
    turns = resp.json()["transcript"]
    patient_turn = next(t for t in turns if t["role"] == "patient")
    text = patient_turn["text"]

    # Raw PII must be gone; placeholders present.
    assert "415-555-1234" not in text
    assert "maria@x.com" not in text
    assert "Maria" not in text and "Lopez" not in text
    assert "[phone]" in text
    assert "[email]" in text
    assert "[name]" in text
