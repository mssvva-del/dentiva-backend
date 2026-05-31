"""Tests for GET /api/dashboard/* endpoints."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_get_dashboard_briefing_requires_auth(client):
    resp = await client.get("/api/dashboard/briefing")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_dashboard_briefing(client, db_session):
    org_id = _uid("org_briefing")
    user_id = _uid("user_briefing")
    await seed_practice(
        db_session,
        name="Briefing Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/dashboard/briefing",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Required top-level keys
    assert "text" in data
    assert len(data["text"]) > 10

    assert "stats" in data
    stats = data["stats"]
    assert "calls_today" in stats
    assert "calls_answered_by_ai" in stats
    assert "calls_missed" in stats
    assert "bookings_made_today" in stats
    assert "upcoming_appointments_today" in stats

    assert "peak_hours" in data
    assert isinstance(data["peak_hours"], list)

    assert "generated_at" in data
    assert "ai_generated" in data
    # ai_generated may be False if Groq isn't reachable — both values are valid
    assert isinstance(data["ai_generated"], bool)


# ── /weekly ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_weekly_stats_requires_auth(client):
    resp = await client.get("/api/dashboard/weekly")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_weekly_stats(client, db_session):
    org_id = _uid("org_weekly")
    user_id = _uid("user_weekly")
    await seed_practice(
        db_session,
        name="Weekly Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/dashboard/weekly",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "days" in data
    assert len(data["days"]) == 7

    for day in data["days"]:
        assert "date" in day
        assert "calls_total" in day
        assert "calls_answered_by_ai" in day
        assert "calls_missed" in day
        assert "bookings_created" in day
        assert "avg_duration_seconds" in day

    assert "totals" in data
    totals = data["totals"]
    assert "calls_total" in totals
    assert "calls_answered_by_ai" in totals
    assert "calls_missed" in totals
    assert "bookings_created" in totals
    assert "ai_answer_rate" in totals


# ── /calls-by-hour ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_calls_by_hour_requires_auth(client):
    resp = await client.get("/api/dashboard/calls-by-hour")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_calls_by_hour(client, db_session):
    org_id = _uid("org_cbh")
    user_id = _uid("user_cbh")
    await seed_practice(
        db_session,
        name="Calls By Hour Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/dashboard/calls-by-hour",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "hours" in data
    assert len(data["hours"]) == 24

    # Verify all 24 hours are present in order
    for i, entry in enumerate(data["hours"]):
        assert entry["hour"] == i
        assert isinstance(entry["count"], int)

    assert "peak_hour" in data
    assert "peak_count" in data
    assert 0 <= data["peak_hour"] <= 23


# ── /conversion ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conversion_requires_auth(client):
    resp = await client.get("/api/dashboard/conversion")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_conversion(client, db_session):
    org_id = _uid("org_conv")
    user_id = _uid("user_conv")
    await seed_practice(
        db_session,
        name="Conversion Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/dashboard/conversion",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "period_days" in data
    assert data["period_days"] == 30
    assert "calls_total" in data
    assert "calls_completed" in data
    assert "calls_with_booking_intent" in data
    assert "bookings_created" in data
    assert "conversion_rate" in data
    assert "ai_answer_rate" in data
    assert "avg_call_duration_seconds" in data
    assert "top_procedures" in data
    assert isinstance(data["top_procedures"], list)


# ── /roi ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_roi_requires_auth(client):
    resp = await client.get("/api/dashboard/roi")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_roi(client, db_session):
    org_id = _uid("org_roi")
    user_id = _uid("user_roi")
    await seed_practice(
        db_session,
        name="ROI Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/dashboard/roi",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Required keys
    assert data["period_days"] == 30
    assert "calls_handled_by_ai" in data
    assert "calls_missed" in data
    assert "bookings_by_ai" in data
    assert "total_talk_time_minutes" in data
    assert "minutes_saved" in data
    assert "cost_saved_usd" in data
    assert "revenue_protected_usd" in data
    assert "ai_answer_rate_pct" in data

    # Values must be non-negative
    assert data["cost_saved_usd"] >= 0
    assert data["revenue_protected_usd"] >= 0
    assert data["calls_handled_by_ai"] >= 0
    assert data["calls_missed"] >= 0
    assert data["bookings_by_ai"] >= 0
    assert data["total_talk_time_minutes"] >= 0
    assert data["minutes_saved"] >= 0
    assert 0.0 <= data["ai_answer_rate_pct"] <= 100.0


# ── /activity ─────────────────────────────────────────────────────────────────

_ACT_FUTURE = "2099-11-10"
_ACT_FUTURE_2 = "2099-11-18"
_ACT_PHONE = "+15558881234"


@pytest.mark.asyncio
async def test_get_activity_requires_auth(client):
    resp = await client.get("/api/dashboard/activity")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_activity_empty_practice(client, db_session):
    org_id = _uid("org_act")
    user_id = _uid("user_act")
    await seed_practice(
        db_session,
        name="Activity Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    resp = await client.get(
        "/api/dashboard/activity",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["period_days"] == 30
    assert len(data["days"]) == 30
    assert data["created"] == 0
    assert data["rescheduled"] == 0
    assert data["cancelled"] == 0
    assert data["net_added"] == 0
    assert data["reschedule_rate"] == 0.0
    assert data["cancellation_rate"] == 0.0
    for day in data["days"]:
        assert {"date", "created", "rescheduled", "cancelled"} <= day.keys()


@pytest.mark.asyncio
async def test_activity_counts_create_reschedule_cancel(client, db_session):
    """Drive a booking through book → reschedule → cancel via the voice webhook
    and assert /activity reflects all three audit actions."""
    org_id = _uid("org_actflow")
    user_id = _uid("user_actflow")
    await seed_practice(
        db_session,
        name="Activity Flow Dental",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    # 1) book
    book = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "act-call-1", "agent_id": "agent_act"},
            "name": "book_appointment",
            "args": {
                "patient_first_name": "Nina",
                "patient_last_name": "Park",
                "patient_phone": _ACT_PHONE,
                "procedure": "cleaning",
                "preferred_date": _ACT_FUTURE,
                "preferred_time_window": "morning",
            },
        },
    )
    assert book.status_code == 200 and book.json()["booked"] is True

    # 2) reschedule
    resched = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "act-call-1", "agent_id": "agent_act"},
            "name": "reschedule_appointment",
            "args": {
                "patient_phone": _ACT_PHONE,
                "new_date": _ACT_FUTURE_2,
                "new_time_window": "afternoon",
            },
        },
    )
    assert resched.status_code == 200 and resched.json()["rescheduled"] is True

    # 3) cancel
    cancel = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "act-call-1", "agent_id": "agent_act"},
            "name": "cancel_appointment",
            "args": {"patient_phone": _ACT_PHONE, "reason": "test"},
        },
    )
    assert cancel.status_code == 200 and cancel.json()["cancelled"] is True

    await db_session.commit()

    resp = await client.get(
        "/api/dashboard/activity",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["created"] == 1
    assert data["rescheduled"] == 1
    assert data["cancelled"] == 1
    assert data["net_added"] == 0  # 1 created − 1 cancelled
    assert data["reschedule_rate"] == 1.0
    assert data["cancellation_rate"] == 1.0
    # Today's bucket should carry all three.
    today_bucket = data["days"][-1]
    assert today_bucket["created"] == 1
    assert today_bucket["rescheduled"] == 1
    assert today_bucket["cancelled"] == 1
