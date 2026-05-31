"""Phase 2 webhook persistence tests.

Tests cover:
- call_started → calls row created
- call_started idempotency (duplicate = silent no-op)
- call_ended → calls row updated with transcript / status
- function_call book_appointment → booking row + audit_log created
- GET /api/calls/:call_id → returns full detail with transcript
- GET /api/calls/:call_id → 404 for wrong tenant
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from tests.conftest import seed_practice


# ---------------------------------------------------------------------------
# call_started
# ---------------------------------------------------------------------------


async def test_call_started_creates_call_row(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Smile NJ", clerk_org_id="org_cs1", clerk_user_id="user_cs1"
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_started",
            "call_id": "retell-test-001",
            "call": {
                "agent_id": None,
                "from_number": "+15551112222",
                "to_number": "+15559876543",
                "start_timestamp": 1748563200000,  # Unix ms
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify a call row was written.
    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-test-001")
    )
    call = result.scalar_one_or_none()
    assert call is not None
    assert call.status == "in_progress"
    assert call.from_number == "+15551112222"
    assert call.direction == "inbound"


async def test_call_started_idempotent(client, db_session):
    """Second call_started with same call_id does NOT create a duplicate row."""
    await seed_practice(
        db_session, name="Idempotent Dental", clerk_org_id="org_idem", clerk_user_id="user_idem"
    )
    payload = {
        "event": "call_started",
        "call_id": "retell-idem-001",
        "call": {
            "from_number": "+15550001111",
            "to_number": "+15559876543",
        },
    }

    resp1 = await client.post("/webhooks/retell", json=payload)
    resp2 = await client.post("/webhooks/retell", json=payload)

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-idem-001")
    )
    calls = result.scalars().all()
    assert len(calls) == 1, "Exactly one call row must exist after duplicate call_started"


# ---------------------------------------------------------------------------
# call_ended
# ---------------------------------------------------------------------------


async def test_call_ended_updates_call_row(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Smile CE", clerk_org_id="org_ce1", clerk_user_id="user_ce1"
    )

    # First create the call row via call_started.
    await client.post(
        "/webhooks/retell",
        json={
            "event": "call_started",
            "call_id": "retell-ce-001",
            "call": {
                "from_number": "+15553334444",
                "to_number": "+15559876543",
                "start_timestamp": 1748563200000,
            },
        },
    )

    # Now end it with a transcript.
    transcript = [
        {"role": "agent", "content": "Thank you for calling, how can I help?"},
        {"role": "user", "content": "I need a cleaning appointment."},
    ]
    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_ended",
            "call_id": "retell-ce-001",
            "call": {
                "start_timestamp": 1748563200000,
                "end_timestamp": 1748563342000,  # 142 seconds later
                "disconnection_reason": "user_hangup",
                "transcript": transcript,
            },
        },
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-ce-001")
    )
    # Refresh from DB.
    await db_session.commit()
    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-ce-001")
    )
    call = result.scalar_one_or_none()
    assert call is not None
    assert call.status == "completed"
    assert call.duration_seconds == 142
    assert call.transcript_jsonb is not None
    assert isinstance(call.transcript_jsonb, list)


async def test_call_ended_saves_recording_url(client, db_session):
    """call_ended with recording_url in payload saves it to recording_path on the call row."""
    practice, _ = await seed_practice(
        db_session, name="Rec Dental", clerk_org_id="org_rec1", clerk_user_id="user_rec1"
    )

    # Create the call row first.
    await client.post(
        "/webhooks/retell",
        json={
            "event": "call_started",
            "call_id": "retell-rec-001",
            "call": {
                "from_number": "+15557778888",
                "to_number": "+15559876543",
                "start_timestamp": 1748563200000,
            },
        },
    )

    recording_url = "https://storage.retellai.com/recordings/retell-rec-001.mp3"
    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_ended",
            "call_id": "retell-rec-001",
            "call": {
                "start_timestamp": 1748563200000,
                "end_timestamp": 1748563320000,
                "disconnection_reason": "user_hangup",
                "recording_url": recording_url,
                "detected_language": "en-US",
            },
        },
    )
    assert resp.status_code == 200

    await db_session.commit()
    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-rec-001")
    )
    call = result.scalar_one_or_none()
    assert call is not None
    assert call.recording_path == recording_url
    assert call.language_detected == "en-US"


async def test_call_ended_without_prior_started(client, db_session):
    """call_ended on an unknown call creates a call row gracefully."""
    await seed_practice(
        db_session, name="Orphan Dental", clerk_org_id="org_orp", clerk_user_id="user_orp"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_ended",
            "call_id": "retell-orphan-999",
            "call": {
                "disconnection_reason": "user_hangup",
            },
        },
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# function_call book_appointment → creates booking + audit_log
# ---------------------------------------------------------------------------


async def test_book_appointment_creates_booking_and_audit(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Booking Dental", clerk_org_id="org_bk1", clerk_user_id="user_bk1"
    )

    # First place a call_started so call row exists.
    await client.post(
        "/webhooks/retell",
        json={
            "event": "call_started",
            "call_id": "retell-bk-001",
            "call": {
                "from_number": "+15551234567",
                "to_number": "+15559876543",
            },
        },
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": "retell-bk-001",
            "function_name": "book_appointment",
            "args": {
                "patient_first_name": "Maria",
                "patient_last_name": "Garcia",
                "patient_phone": "+15551234567",
                "procedure": "cleaning",
                "preferred_date": "2026-06-05",
                "preferred_time_window": "morning",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # New contract: a flat confirmation the voice LLM can speak back, plus the
    # candidate slots at top level (no "result" wrapper).
    assert body["booked"] is True
    assert "appointment" in body and body["appointment"]["procedure"] == "cleaning"
    slots = body["available_slots"]
    assert len(slots) >= 1
    assert "date" in slots[0] and "time" in slots[0] and "provider" in slots[0]

    # Verify booking row persisted.
    await db_session.commit()
    result = await db_session.execute(
        select(Booking).where(Booking.practice_id == practice.id)
    )
    bookings = result.scalars().all()
    assert len(bookings) == 1
    assert bookings[0].procedure_type == "cleaning"
    assert bookings[0].source == "ai_call"

    # Verify audit log.
    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.practice_id == practice.id, AuditLog.action == "booking_created"
        )
    )
    audit = result.scalar_one_or_none()
    assert audit is not None
    assert audit.resource_type == "booking"


async def test_custom_tool_shape_books_appointment(client, db_session):
    """Retell custom tools POST {call, name, args} with NO 'event' field.

    Regression guard: web calls invoke tools in this shape; the handler must
    route it to the same dispatcher as the legacy function_call event.
    """
    practice, _ = await seed_practice(
        db_session, name="WebCall Dental", clerk_org_id="org_wc1", clerk_user_id="user_wc1"
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "web-call-xyz", "agent_id": "agent_web"},
            "name": "book_appointment",
            "args": {
                "patient_first_name": "Alex",
                "patient_last_name": "Nguyen",
                "patient_phone": "+15550001111",
                "procedure": "cleaning",
                "preferred_date": "2026-06-10",
                "preferred_time_window": "afternoon",
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["booked"] is True

    await db_session.commit()
    bookings = (
        await db_session.execute(
            select(Booking).where(Booking.practice_id == practice.id)
        )
    ).scalars().all()
    assert len(bookings) == 1
    assert bookings[0].source == "ai_call"


# ---------------------------------------------------------------------------
# GET /api/calls/:call_id
# ---------------------------------------------------------------------------


async def test_get_call_detail_happy_path(client, db_session):
    practice, user = await seed_practice(
        db_session, name="Detail Dental", clerk_org_id="org_det1", clerk_user_id="user_det1"
    )

    # Seed a call row directly.
    call = Call(
        id=uuid.uuid4(),
        practice_id=practice.id,
        retell_call_id="retell-det-001",
        direction="inbound",
        from_number="+15551112222",
        to_number="+15559876543",
        started_at=datetime.now(tz=UTC),
        status="completed",
        transcript_jsonb=[
            {"role": "agent", "content": "Thank you for calling, how can I help?"},
            {"role": "user", "content": "I need a cleaning."},
        ],
    )
    db_session.add(call)
    await db_session.commit()

    resp = await client.get(
        f"/api/calls/{call.id}",
        headers={"X-Dev-Clerk-User-Id": "user_det1", "X-Dev-Clerk-Org-Id": "org_det1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(call.id)
    assert body["status"] == "completed"
    assert isinstance(body["transcript"], list)
    assert len(body["transcript"]) == 2
    assert body["transcript"][0]["role"] == "agent"
    assert "Thank you for calling" in body["transcript"][0]["text"]


async def test_get_call_detail_not_found(client, db_session):
    """Non-existent call_id returns 404."""
    _, _ = await seed_practice(
        db_session, name="404 Dental", clerk_org_id="org_404", clerk_user_id="user_404"
    )
    resp = await client.get(
        f"/api/calls/{uuid.uuid4()}",
        headers={"X-Dev-Clerk-User-Id": "user_404", "X-Dev-Clerk-Org-Id": "org_404"},
    )
    assert resp.status_code == 404


async def test_get_call_detail_wrong_tenant(client, db_session):
    """Call owned by practice A is invisible to practice B — returns 404."""
    practice_a, _ = await seed_practice(
        db_session, name="Practice A", clerk_org_id="org_a_det", clerk_user_id="user_a_det"
    )
    _, _ = await seed_practice(
        db_session, name="Practice B", clerk_org_id="org_b_det", clerk_user_id="user_b_det"
    )

    call = Call(
        id=uuid.uuid4(),
        practice_id=practice_a.id,
        retell_call_id="retell-ta-001",
        direction="inbound",
        from_number="+15550000001",
        to_number="+15559876543",
        started_at=datetime.now(tz=UTC),
        status="completed",
    )
    db_session.add(call)
    await db_session.commit()

    # Practice B user tries to access Practice A call.
    resp = await client.get(
        f"/api/calls/{call.id}",
        headers={"X-Dev-Clerk-User-Id": "user_b_det", "X-Dev-Clerk-Org-Id": "org_b_det"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket relay — unit-level tests (mocked Groq, no real HTTP)
# Uses synchronous Starlette TestClient which has native WS support.
# ---------------------------------------------------------------------------


async def test_ws_retell_llm_call_details_ack(_prepare_database):
    """call_details interaction type is acknowledged without crashing.

    Uses anyio to run a real WebSocket connection against the ASGI app in-process.
    No DB operations; purely tests the WS routing layer.
    """
    import json

    import anyio
    from starlette.testclient import TestClient  # type: ignore[import]

    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    from app.main import app

    # Run synchronous TestClient inside anyio's thread to avoid event-loop clash.
    def _run_ws_test():
        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/retell-llm") as ws:
                ws.send_text(
                    json.dumps(
                        {"interaction_type": "call_details", "call": {"call_id": "t1"}}
                    )
                )
                ws.send_text(
                    json.dumps(
                        {"interaction_type": "call_details", "call": {"call_id": "t1"}}
                    )
                )
                # No exception = server handled call_details gracefully.

    await anyio.to_thread.run_sync(_run_ws_test)


async def test_ws_retell_llm_response_required_mocked(_prepare_database, monkeypatch):
    """response_required triggers LLM relay; Groq is mocked to avoid real HTTP."""
    import json

    import anyio
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    from app.api import retell_llm_relay
    from app.main import app

    captured: list = []

    async def mock_stream(websocket, response_id, transcript):
        await websocket.send_text(
            json.dumps(
                {
                    "response_id": response_id,
                    "content": "Hello! How can I help you today?",
                    "content_complete": False,
                }
            )
        )
        await websocket.send_text(
            json.dumps({"response_id": response_id, "content": "", "content_complete": True})
        )

    monkeypatch.setattr(retell_llm_relay, "_stream_groq_response", mock_stream)

    def _run_ws_test():
        from starlette.testclient import TestClient  # type: ignore[import]

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/retell-llm") as ws:
                ws.send_text(
                    json.dumps(
                        {
                            "interaction_type": "response_required",
                            "response_id": 1,
                            "transcript": [{"role": "user", "content": "I need a cleaning."}],
                        }
                    )
                )
                while True:
                    raw = ws.receive_text()
                    chunk = json.loads(raw)
                    captured.append(chunk)
                    if chunk.get("content_complete"):
                        break

    await anyio.to_thread.run_sync(_run_ws_test)

    assert len(captured) >= 1
    full_text = "".join(c.get("content", "") for c in captured)
    assert "Hello" in full_text
    assert all(c["response_id"] == 1 for c in captured)
