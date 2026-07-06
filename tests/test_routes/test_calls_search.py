"""Tests for search param on GET /api/calls endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db import set_tenant
from app.models.call import Call
from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def _seed_call(
    db_session,
    practice,
    *,
    retell_call_id: str,
    from_number: str = "+15551112222",
    to_number: str = "+15559998888",
    status: str = "completed",
) -> Call:
    await set_tenant(db_session, practice.id)
    call = Call(
        id=uuid.uuid4(),
        practice_id=practice.id,
        retell_call_id=retell_call_id,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        started_at=datetime.now(UTC),
        status=status,
    )
    db_session.add(call)
    await db_session.commit()
    return call


@pytest.mark.asyncio
async def test_calls_search_matches_from_number(client, db_session):
    """Search by the caller's full number (any format) returns matching calls.

    from_number is encrypted now, so search matches the deterministic hash — exact
    on the normalized number, not a substring (see B3)."""
    org_id = _uid("org_srch1")
    user_id = _uid("user_srch1")
    practice, _ = await seed_practice(
        db_session,
        name="Search Dental 1",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    await _seed_call(
        db_session,
        practice,
        retell_call_id=_uid("ret_match"),
        from_number="+15551234567",
        to_number="+18005550100",
    )
    await _seed_call(
        db_session,
        practice,
        retell_call_id=_uid("ret_nomatch"),
        from_number="+12125550199",
        to_number="+18005550100",
    )

    # Full number, differently formatted → matches via the normalized hash.
    resp = await client.get(
        "/api/calls",
        params={"search": "(555) 123-4567"},
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["calls"][0]["from_number"] == "+15551234567"


@pytest.mark.asyncio
async def test_calls_search_no_match_returns_empty(client, db_session):
    """Search with a term that matches nothing returns an empty list."""
    org_id = _uid("org_srch2")
    user_id = _uid("user_srch2")
    practice, _ = await seed_practice(
        db_session,
        name="Search Dental 2",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    await _seed_call(
        db_session,
        practice,
        retell_call_id=_uid("ret_any"),
        from_number="+15557779999",
        to_number="+18005550100",
    )

    resp = await client.get(
        "/api/calls?search=nonexistent99999",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["calls"] == []


@pytest.mark.asyncio
async def test_calls_search_ignores_to_number(client, db_session):
    """Search matches the CALLER number only. to_number (our own inbound line) is
    not indexed for search after B3 — searching it returns nothing, by design."""
    org_id = _uid("org_srch3")
    user_id = _uid("user_srch3")
    practice, _ = await seed_practice(
        db_session,
        name="Search Dental 3",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    await _seed_call(
        db_session,
        practice,
        retell_call_id=_uid("ret_to"),
        from_number="+12125550100",
        to_number="+15559876543",
    )

    # Searching the to_number → no match (only from_number/caller is searchable).
    resp = await client.get(
        "/api/calls?search=+15559876543",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    # But searching the caller's from_number → matches.
    resp2 = await client.get(
        "/api/calls?search=+12125550100",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp2.json()["total"] == 1
