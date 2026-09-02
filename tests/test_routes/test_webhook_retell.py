"""Phase 1 webhook tests — kept as-is for regression coverage.

Note: these tests use the `client` fixture which ensures a fresh DB engine,
so each test gets a clean connection pool via _prepare_database.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from tests.conftest import seed_practice


async def test_book_appointment_function_call(client, db_session):
    # No practice seeded — nothing to compute availability against, so the webhook
    # returns an empty slot list (never invents times) and does not persist.
    payload = {
        "event": "function_call",
        "call_id": "retell_call_xyz",
        "function_name": "book_appointment",
        "args": {
            "patient_first_name": "Maria",
            "patient_last_name": "Garcia",
            "patient_phone": "+15551234567",
            "procedure": "cleaning",
            "preferred_date": "2026-06-05",
            "preferred_time_window": "morning",
        },
    }
    resp = await client.post("/webhooks/retell", json=payload)
    assert resp.status_code == 200
    assert resp.json()["result"]["available_slots"] == []


async def test_call_started_ack(client, db_session):
    # Seeds a practice so the call_started can persist the call row.
    await seed_practice(
        db_session, name="Ack Dental", clerk_org_id="org_ack", clerk_user_id="user_ack"
    )
    resp = await client.post(
        "/webhooks/retell", json={"event": "call_started", "call_id": "c1"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_spoken_slot_names_the_weekday():
    # The agent must not derive the weekday from an ISO date itself — in a
    # scripted replay it offered a Thursday slot as "Wednesday the third" and
    # then told the caller Thursday at ten was unavailable while holding it.
    from app.webhooks.retell import _spoken_slot

    assert _spoken_slot("2026-09-03", "10:00") == "Thursday, September 3 at 10:00 AM"
    assert _spoken_slot("2026-09-03", "14:00") == "Thursday, September 3 at 2:00 PM"
    assert _spoken_slot("2026-09-03", "00:30") == "Thursday, September 3 at 12:30 AM"
    assert _spoken_slot("2026-09-03", "12:00") == "Thursday, September 3 at 12:00 PM"
    # Junk degrades to the raw values instead of raising mid-call.
    assert _spoken_slot("not-a-date", "10:00") == "not-a-date 10:00"


async def test_a_rejected_webhook_is_recorded_not_swallowed(client, monkeypatch):
    """A webhook we refuse must leave a trace.

    Every call since 1 June sat at status "in_progress" — no duration, no
    transcript, no metered minutes — because the call lifecycle events stopped
    landing. From the outside "the vendor went quiet" and "we started rejecting
    a valid signature" looked exactly the same, and that ambiguity cost hours.
    """
    from app.observability import alerts as alert_store
    from app.webhooks import retell as retell_mod

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        retell_mod, "record_alert", lambda k, d="", **kw: seen.append((k, d))
    )
    monkeypatch.setattr(retell_mod, "_verify_signature", lambda *a, **k: False)

    r = await client.post("/webhooks/retell", json={"event": "call_ended"})
    assert r.status_code == 401
    assert any(k == "webhook_signature_rejected" for k, _ in seen), seen
    # The heartbeat is deliberately non-paging: it fires on every healthy
    # webhook and would otherwise train everyone to ignore the alert list.
    assert "webhook_event_seen" in alert_store.NON_PAGING_KINDS


async def test_a_failing_lifecycle_handler_names_itself(client, monkeypatch):
    """Retell retries a 5xx. If the handler fails the same way five times and
    says nothing, the call sits frozen at "in progress" and the alert list stays
    empty — which is exactly what happened for three months.

    The alert carries the exception TYPE and the frame, never the message: a
    call_ended payload is full of patient data.
    """
    from app.webhooks import retell as retell_mod

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        retell_mod, "record_alert", lambda k, d="", **kw: seen.append((k, d))
    )
    monkeypatch.setattr(retell_mod, "_verify_signature", lambda *a, **k: True)

    async def boom(_payload):
        raise ValueError("Jane Doe +16175551234")

    monkeypatch.setattr(retell_mod, "_handle_call_ended", boom)

    with pytest.raises(ValueError):
        await client.post("/webhooks/retell", json={"event": "call_ended"})

    failures = [d for k, d in seen if k == "webhook_handler_failed"]
    assert failures, seen
    assert "exc=ValueError" in failures[0]
    assert "event=call_ended" in failures[0]
    # The payload's contents must not travel with the alert.
    assert "Jane" not in failures[0] and "6175551234" not in failures[0]


async def test_call_ended_finishes_the_call_when_the_payload_cannot_name_the_clinic(
    client, db_session, monkeypatch
):
    """The three-month outage, pinned.

    call_ended resolved the tenant from its own payload. When that came back
    empty the tenant stayed unbound, RLS hid the existing row, and the handler
    took its "orphan" branch — which invented practice_id=uuid4(). That is a
    guaranteed foreign-key violation: 500, five Retell retries, five identical
    failures, and a call left at "in progress" with no duration, no transcript
    and no metered minutes. 620 calls in that state, /health green throughout.

    The row itself knows the tenant — it was written while the dialled number
    was still available. Ask it.
    """
    from app.models.call import Call
    from app.webhooks import retell as retell_mod

    practice, _user = await seed_practice(
        db_session, name="Finish Me Dental", clerk_org_id="org_finish",
        clerk_user_id="user_finish",
    )
    db_session.add(Call(
        id=uuid.uuid4(), practice_id=practice.id, retell_call_id="call_finish_me",
        direction="inbound", from_number="+15550001111", to_number="+15550002222",
        started_at=datetime.now(tz=UTC), status="in_progress",
    ))
    await db_session.commit()

    # Exactly the failing condition: the payload tells us nothing about which
    # clinic this is.
    monkeypatch.setattr(retell_mod, "_verify_signature", lambda *a, **k: True)

    async def unresolvable(_call_data, _agent_id):
        return None

    monkeypatch.setattr(retell_mod, "_resolve_practice_meta", unresolvable)

    r = await client.post("/webhooks/retell", json={
        "event": "call_ended",
        "call_id": "call_finish_me",
        "call": {
            "call_id": "call_finish_me",
            "start_timestamp": 1_788_000_000_000,
            "end_timestamp": 1_788_000_090_000,
            "disconnection_reason": "user_hangup",
        },
    })
    assert r.status_code == 200, r.text

    await db_session.commit()  # see the handler's write
    row = (await db_session.execute(
        select(Call).where(Call.retell_call_id == "call_finish_me")
    )).scalar_one()
    await db_session.refresh(row)
    assert row.status == "completed", "the call never left 'in progress'"
    assert row.duration_seconds == 90
    assert row.ended_at is not None
    # And exactly one row: the orphan branch must not have minted a second.
    count = (await db_session.execute(
        select(func.count()).select_from(Call)
        .where(Call.retell_call_id == "call_finish_me")
    )).scalar_one()
    assert count == 1


async def test_our_own_forgery_probe_is_refused_quietly(client, monkeypatch):
    """The monitor pokes this endpoint every hour with no signature to prove a
    forgery is refused. Refusing it is the PASS condition — and it was raising a
    paging alert, so production sat at "degraded" around the clock and the real
    alerts arrived into a list nobody trusted.

    Still 401. Only the loudness changes, so an attacker who copies the header
    gains nothing but a quieter line in a log we still keep.
    """
    from app.webhooks import retell as retell_mod

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        retell_mod, "record_alert", lambda k, d="", **kw: seen.append((k, d))
    )
    monkeypatch.setattr(retell_mod, "_verify_signature", lambda *a, **k: False)

    r = await client.post(
        "/webhooks/retell", json={"event": "call_ended"},
        headers={"User-Agent": "dentovox-monitor"},
    )
    assert r.status_code == 401
    kinds = [k for k, _ in seen]
    assert "webhook_forgery_probe_refused" in kinds
    assert "webhook_signature_rejected" not in kinds, (
        "our own passing self-test must not page"
    )
