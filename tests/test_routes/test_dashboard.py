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
