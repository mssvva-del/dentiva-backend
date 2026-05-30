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
    """Search by a phone number substring returns only matching calls."""
    org_id = _uid("org_srch1")
    user_id = _uid("user_srch1")
    practice, _ = await seed_practice(
        db_session,
        name="Search Dental 1",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )

    # Seed a call that SHOULD match (+1555…)
    await _seed_call(
        db_session,
        practice,
        retell_call_id=_uid("ret_match"),
        from_number="+15551234567",
        to_number="+18005550100",
    )
    # Seed a call that should NOT match
    await _seed_call(
        db_session,
        practice,
        retell_call_id=_uid("ret_nomatch"),
        from_number="+12125550199",
        to_number="+18005550100",
    )

    resp = await client.get(
        "/api/calls",
        params={"search": "+1555"},
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Only the matching call should be returned
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
async def test_calls_search_matches_to_number(client, db_session):
    """Search term can match the to_number field as well."""
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

    resp = await client.get(
        "/api/calls?search=987",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["calls"][0]["to_number"] == "+15559876543"
