"""Tests for GET /api/dashboard/briefing."""

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
