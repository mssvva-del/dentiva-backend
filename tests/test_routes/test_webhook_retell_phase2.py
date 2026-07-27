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

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.callback_request import CallbackRequest
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
    # No intent known yet + real conversation + no booking → info_only.
    assert call.outcome == "info_only"


async def test_call_ended_short_call_is_abandoned(client, db_session):
    """A call that connects but drops in seconds classifies as abandoned, not info_only."""
    await seed_practice(
        db_session, name="Abandon DC", clerk_org_id="org_ab1", clerk_user_id="user_ab1"
    )
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "retell-ab-001",
        "call": {"from_number": "+15550001111", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    await client.post("/webhooks/retell", json={
        "event": "call_ended", "call_id": "retell-ab-001",
        "call": {"start_timestamp": 1748563200000,
                 "end_timestamp": 1748563204000,  # 4 seconds
                 "disconnection_reason": "user_hangup",
                 "transcript": [{"role": "agent", "content": "Thanks for calling…"}]},
    })
    await db_session.commit()
    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-ab-001")
    )).scalar_one()
    assert call.outcome == "abandoned"


async def test_call_analyzed_upgrades_info_only_to_no_booking(client, db_session):
    """call_ended can only guess info_only; call_analyzed learns the caller wanted an
    appointment (intent) and refines the outcome to the lost-booking signal."""
    await seed_practice(
        db_session, name="Lost Booking DC", clerk_org_id="org_lb1", clerk_user_id="user_lb1"
    )
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "retell-lb-001",
        "call": {"from_number": "+15552223333", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    await client.post("/webhooks/retell", json={
        "event": "call_ended", "call_id": "retell-lb-001",
        "call": {"start_timestamp": 1748563200000,
                 "end_timestamp": 1748563290000,  # 90s real conversation
                 "disconnection_reason": "user_hangup",
                 "transcript": [{"role": "user", "content": "I wanted to book a cleaning."}]},
    })
    await db_session.commit()
    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-lb-001")
    )).scalar_one()
    assert call.outcome == "info_only"  # intent unknown at hang-up

    # Now the analysis arrives: the caller was trying to book.
    await client.post("/webhooks/retell", json={
        "event": "call_analyzed", "call_id": "retell-lb-001",
        "call_analysis": {"custom_analysis_data": {"intent": "book_appointment"}},
    })
    await db_session.commit()
    db_session.expire_all()  # drop the identity-map cache so we read the fresh row
    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-lb-001")
    )).scalar_one()
    assert call.outcome == "no_booking"  # refined → lost-booking signal for QA


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

    # Native availability only offers REAL future openings within business hours —
    # use a weekday a few days out (seed_practice sets Mon–Fri 09:00–18:00).
    from datetime import date, timedelta
    future = date.today() + timedelta(days=3)
    while future.weekday() >= 5:  # skip Sat/Sun
        future += timedelta(days=1)

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
                "preferred_date": future.isoformat(),
                "preferred_time_window": "morning",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # Flat confirmation the voice LLM can speak back.
    assert body["booked"] is True
    assert "appointment" in body and body["appointment"]["procedure"] == "cleaning"
    assert body["appointment"]["date"] == future.isoformat()

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
# PROGRAMMATIC EMERGENCY LOCK
#
# The lock lives in the tool router (app/webhooks/retell.py), NOT in the prompt:
# the LLM can be talked out of "emergency mode", so scheduling is refused at the
# backend regardless of what the LLM decides. State is persisted in
# calls.emergency_active so it survives restarts and spans every tool call in
# one phone call. Double trigger: (a) create_callback_request(urgent=true), OR
# (b) deterministic keyword scan of ANY tool's args.
# ---------------------------------------------------------------------------


async def test_emergency_blocks_booking_after_urgent_callback(client, db_session):
    """After an urgent callback, book_appointment is physically refused.

    Trigger (a): the LLM set urgent=true. The reason here has NO keyword, so
    this isolates the urgent-flag path.
    """
    await seed_practice(
        db_session, name="Emerg One", clerk_org_id="org_e1", clerk_user_id="user_e1"
    )
    call_id = "retell-emerg-001"

    r1 = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "create_callback_request",
            "args": {
                "patient_phone": "+15551234567",
                "reason": "caller is very distressed and needs help now",
                "urgent": True,
            },
        },
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "callback_logged"

    r2 = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "book_appointment",
            "args": {
                "patient_first_name": "Bob",
                "patient_last_name": "Lee",
                "patient_phone": "+15551234567",
                "procedure": "cleaning",
                "preferred_date": "2026-06-05",
                "preferred_time_window": "morning",
            },
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["blocked"] is True
    assert body["reason"] == "emergency_active"
    assert "urgent situation" in body["message"]

    # No booking row was created.
    await db_session.commit()
    bookings = (await db_session.execute(select(Booking))).scalars().all()
    assert len(bookings) == 0

    # The flag is persisted on the call row.
    call = (
        await db_session.execute(select(Call).where(Call.retell_call_id == call_id))
    ).scalar_one()
    assert call.emergency_active is True


async def test_emergency_flag_persists_across_multiple_webhook_calls(client, db_session):
    """The flag survives many tool calls within ONE phone call (DB-persisted)."""
    await seed_practice(
        db_session, name="Emerg Two", clerk_org_id="org_e2", clerk_user_id="user_e2"
    )
    call_id = "retell-emerg-002"

    # Engage emergency.
    await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "create_callback_request",
            "args": {
                "patient_phone": "+15551234567",
                "reason": "severe pain and swelling",
                "urgent": True,
            },
        },
    )

    # Several intervening tool calls — the scheduling tools stay refused every time.
    for _ in range(3):
        r = await client.post(
            "/webhooks/retell",
            json={
                "event": "function_call",
                "call_id": call_id,
                "function_name": "check_availability",
                "args": {"procedure": "cleaning", "preferred_date": "2026-06-05"},
            },
        )
        assert r.status_code == 200
        assert r.json()["blocked"] is True

    # A non-scheduling tool (lookup_patient) still works in between.
    rl = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "lookup_patient",
            "args": {"patient_phone": "+15551234567"},
        },
    )
    assert rl.status_code == 200
    assert "blocked" not in rl.json()

    # And booking is STILL blocked after all those webhooks.
    rb = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "book_appointment",
            "args": {
                "patient_first_name": "Ana",
                "patient_last_name": "Diaz",
                "patient_phone": "+15551234567",
                "procedure": "cleaning",
                "preferred_date": "2026-06-06",
                "preferred_time_window": "afternoon",
            },
        },
    )
    assert rb.json()["blocked"] is True

    call = (
        await db_session.execute(select(Call).where(Call.retell_call_id == call_id))
    ).scalar_one()
    assert call.emergency_active is True


async def test_emergency_caught_by_keyword_without_urgent_flag(client, db_session):
    """Backend catches the emergency from keywords even if the LLM forgot urgent.

    Trigger (b): urgent=False, but the reason text says "uncontrolled bleeding".
    The deterministic regex must engage the lock anyway.
    """
    await seed_practice(
        db_session, name="Emerg Three", clerk_org_id="org_e3", clerk_user_id="user_e3"
    )
    call_id = "retell-emerg-003"

    r1 = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "create_callback_request",
            "args": {
                "patient_phone": "+15551234567",
                "reason": "uncontrolled bleeding from the gum",
                "urgent": False,
            },
        },
    )
    assert r1.status_code == 200

    # Even though urgent was False, booking must now be refused.
    r2 = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "book_appointment",
            "args": {
                "patient_first_name": "Sam",
                "patient_last_name": "Roe",
                "patient_phone": "+15551234567",
                "procedure": "cleaning",
                "preferred_date": "2026-06-07",
                "preferred_time_window": "morning",
            },
        },
    )
    assert r2.status_code == 200
    assert r2.json()["blocked"] is True

    call = (
        await db_session.execute(select(Call).where(Call.retell_call_id == call_id))
    ).scalar_one()
    assert call.emergency_active is True


async def test_after_emergency_only_callback_and_transfer_allowed(client, db_session):
    """During an active emergency: scheduling refused; callback + transfer work."""
    await seed_practice(
        db_session, name="Emerg Four", clerk_org_id="org_e4", clerk_user_id="user_e4"
    )
    call_id = "retell-emerg-004"

    # Engage emergency (keyword + urgent).
    await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "create_callback_request",
            "args": {
                "patient_phone": "+15551234567",
                "reason": "knocked out tooth, lots of bleeding",
                "urgent": True,
            },
        },
    )

    # check_availability → blocked.
    rc = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "check_availability",
            "args": {"procedure": "cleaning", "preferred_date": "2026-06-05"},
        },
    )
    assert rc.json()["blocked"] is True

    # book_appointment → blocked.
    rb = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "book_appointment",
            "args": {
                "patient_first_name": "Joy",
                "patient_last_name": "Kim",
                "patient_phone": "+15551234567",
                "procedure": "cleaning",
                "preferred_date": "2026-06-05",
                "preferred_time_window": "morning",
            },
        },
    )
    assert rb.json()["blocked"] is True

    # create_callback_request → still allowed.
    rcb = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "create_callback_request",
            "args": {"patient_phone": "+15551234567", "reason": "follow up", "urgent": True},
        },
    )
    assert rcb.json()["status"] == "callback_logged"

    # transfer_to_human → still allowed.
    rt = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "transfer_to_human",
            "args": {"reason": "emergency: knocked out tooth"},
        },
    )
    assert rt.status_code == 200
    assert rt.json()["status"] == "transfer_initiated"

    # No booking ever created during the emergency.
    await db_session.commit()
    bookings = (await db_session.execute(select(Booking))).scalars().all()
    assert len(bookings) == 0


async def test_create_callback_request_persists_row(client, db_session):
    """create_callback_request must STORE a row (was previously only logged).

    Urgent + linked to the call, with PII encrypted at rest.
    """
    practice, _ = await seed_practice(
        db_session, name="Callback Dental", clerk_org_id="org_cb1", clerk_user_id="user_cb1"
    )
    call_id = "retell-cb-001"

    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "function_call",
            "call_id": call_id,
            "function_name": "create_callback_request",
            "args": {
                "patient_first_name": "Carlos",
                "patient_phone": "+15557654321",
                "reason": "uncontrolled bleeding",
                "urgent": True,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "callback_logged"

    await db_session.commit()
    rows = (
        await db_session.execute(
            select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    cb = rows[0]
    assert cb.urgent is True
    assert cb.status == "pending"
    assert cb.reason == "uncontrolled bleeding"
    # PII decrypts back through the EncryptedString type.
    assert cb.patient_first_name == "Carlos"
    assert cb.phone == "+15557654321"
    # Linked to the call row (created on the fly since web calls skip call_started).
    linked = (
        await db_session.execute(select(Call).where(Call.retell_call_id == call_id))
    ).scalar_one()
    assert cb.call_id == linked.id


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




async def test_web_call_metadata_attributes_to_right_practice(client, db_session):
    """Web calls share the demo agent, so agent_id can't disambiguate with 2+
    clinics. metadata.practice_id must route the call to the correct clinic
    instead of an orphan row (the 'No calls yet' bug)."""
    from app.db import set_tenant
    # Two clinics → _resolve_practice would refuse (None) without metadata.
    await seed_practice(db_session, name="MetaA", clerk_org_id="org_ma", clerk_user_id="u_ma")
    p2, _ = await seed_practice(db_session, name="MetaB", clerk_org_id="org_mb", clerk_user_id="u_mb")
    p2_id = p2.id  # capture before commit expires the instance
    await db_session.commit()  # webhook uses a separate session — must see both

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "retell-meta-1",
        "call": {"from_number": "web", "to_number": "web",
                 "start_timestamp": 1748563200000,
                 "metadata": {"practice_id": str(p2_id)}},  # target the 2nd clinic
    })
    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, p2_id)
    call = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-meta-1")
    )).scalar_one_or_none()
    assert call is not None and call.practice_id == p2_id
