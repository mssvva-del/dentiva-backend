"""Tests for call_analyzed webhook and analysis field exposure in API responses."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.call import Call
from tests.conftest import seed_practice


# ---------------------------------------------------------------------------
# call_analyzed webhook
# ---------------------------------------------------------------------------


async def test_call_analyzed_stores_intent(client, db_session):
    """call_analyzed webhook stores intent, sentiment and escalation on the call row."""
    practice, _ = await seed_practice(
        db_session, name="Analysis Dental", clerk_org_id="org_an1", clerk_user_id="user_an1"
    )

    # First create a call row via call_started.
    await client.post(
        "/webhooks/retell",
        json={
            "event": "call_started",
            "call_id": "retell-an-001",
            "call": {
                "from_number": "+15551112222",
                "to_number": "+15559876543",
                "start_timestamp": 1748563200000,
            },
        },
    )

    # Send call_analyzed with post-call analysis results.
    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_analyzed",
            "call_id": "retell-an-001",
            "call_analysis": {
                "intent": "book_appointment",
                "patient_sentiment": "positive",
                "escalation_needed": False,
                "hipaa_compliant": True,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Refresh from DB and verify the fields were persisted.
    await db_session.commit()
    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-an-001")
    )
    call = result.scalar_one_or_none()
    assert call is not None
    assert call.call_intent == "book_appointment"
    assert call.patient_sentiment == "positive"
    assert call.escalation_needed is False
    assert call.hipaa_compliant is True


async def test_call_analyzed_missing_call(client, db_session):
    """call_analyzed for an unknown retell_call_id returns ok with a warning."""
    await seed_practice(
        db_session, name="NoCall Dental", clerk_org_id="org_nc1", clerk_user_id="user_nc1"
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_analyzed",
            "call_id": "retell-nonexistent-999",
            "call_analysis": {
                "intent": "general_inquiry",
                "patient_sentiment": "neutral",
                "escalation_needed": False,
                "hipaa_compliant": True,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body.get("warning") == "call_not_found"


async def test_call_analyzed_partial_fields(client, db_session):
    """call_analyzed with only some analysis fields only updates those fields."""
    await seed_practice(
        db_session, name="Partial Dental", clerk_org_id="org_pa1", clerk_user_id="user_pa1"
    )

    await client.post(
        "/webhooks/retell",
        json={
            "event": "call_started",
            "call_id": "retell-pa-001",
            "call": {
                "from_number": "+15553334444",
                "to_number": "+15559876543",
            },
        },
    )

    # Only intent is provided; escalation_needed is absent.
    resp = await client.post(
        "/webhooks/retell",
        json={
            "event": "call_analyzed",
            "call_id": "retell-pa-001",
            "call_analysis": {
                "intent": "reschedule",
            },
        },
    )
    assert resp.status_code == 200

    await db_session.commit()
    result = await db_session.execute(
        select(Call).where(Call.retell_call_id == "retell-pa-001")
    )
    call = result.scalar_one_or_none()
    assert call is not None
    assert call.call_intent == "reschedule"
    assert call.escalation_needed is None  # not provided → remains NULL


# ---------------------------------------------------------------------------
# Analysis fields exposed via GET /api/calls
# ---------------------------------------------------------------------------


async def test_list_calls_exposes_analysis_fields(client, db_session):
    """GET /api/calls includes call_intent, patient_sentiment, escalation_needed."""
    practice, _ = await seed_practice(
        db_session, name="Expose Dental", clerk_org_id="org_ex1", clerk_user_id="user_ex1"
    )

    call = Call(
        id=uuid.uuid4(),
        practice_id=practice.id,
        retell_call_id="retell-ex-001",
        direction="inbound",
        from_number="+15551112222",
        to_number="+15559876543",
        started_at=datetime.now(tz=UTC),
        status="completed",
        call_intent="book_appointment",
        patient_sentiment="positive",
        escalation_needed=False,
    )
    db_session.add(call)
    await db_session.commit()

    resp = await client.get(
        "/api/calls",
        headers={"X-Dev-Clerk-User-Id": "user_ex1", "X-Dev-Clerk-Org-Id": "org_ex1"},
    )
    assert resp.status_code == 200
    calls = resp.json()["calls"]
    assert len(calls) == 1
    assert calls[0]["call_intent"] == "book_appointment"
    assert calls[0]["patient_sentiment"] == "positive"
    assert calls[0]["escalation_needed"] is False


async def test_get_call_detail_exposes_analysis_fields(client, db_session):
    """GET /api/calls/:id includes call_intent, patient_sentiment, escalation_needed."""
    practice, _ = await seed_practice(
        db_session, name="Detail2 Dental", clerk_org_id="org_dt2", clerk_user_id="user_dt2"
    )

    call = Call(
        id=uuid.uuid4(),
        practice_id=practice.id,
        retell_call_id="retell-dt2-001",
        direction="inbound",
        from_number="+15551112222",
        to_number="+15559876543",
        started_at=datetime.now(tz=UTC),
        status="completed",
        call_intent="general_inquiry",
        patient_sentiment="negative",
        escalation_needed=True,
    )
    db_session.add(call)
    await db_session.commit()

    resp = await client.get(
        f"/api/calls/{call.id}",
        headers={"X-Dev-Clerk-User-Id": "user_dt2", "X-Dev-Clerk-Org-Id": "org_dt2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["call_intent"] == "general_inquiry"
    assert body["patient_sentiment"] == "negative"
    assert body["escalation_needed"] is True
