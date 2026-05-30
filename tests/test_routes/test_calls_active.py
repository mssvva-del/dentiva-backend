"""Tests for GET /api/calls/active endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db import set_tenant
from app.models.call import Call
from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    """Return a prefix + short UUID suffix to avoid cross-test unique key collisions."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def _seed_call(
    db_session,
    practice,
    *,
    retell_call_id: str,
    status: str = "in_progress",
    started_at: datetime | None = None,
) -> Call:
    """Seed a single call row for the given practice."""
    await set_tenant(db_session, practice.id)
    call = Call(
        id=uuid.uuid4(),
        practice_id=practice.id,
        retell_call_id=retell_call_id,
        direction="inbound",
        from_number="+15551112222",
        to_number="+15559998888",
        started_at=started_at or datetime.now(UTC),
        status=status,
    )
    db_session.add(call)
    await db_session.commit()
    return call


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_active_calls_no_active_returns_empty(client, db_session):
    """When no calls are in_progress the endpoint returns an empty list."""
    org_id = _uid("org_active1")
    user_id = _uid("user_active1")
    practice, _user = await seed_practice(
        db_session,
        name="Active Test Practice",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )
    # Seed a completed call — should NOT appear.
    await _seed_call(
        db_session, practice, retell_call_id=_uid("ret_comp"), status="completed"
    )

    resp = await client.get(
        "/api/calls/active",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["active_calls"] == []


async def test_active_calls_returns_in_progress_call(client, db_session):
    """An in_progress call is returned with a computed duration."""
    org_id = _uid("org_active2")
    user_id = _uid("user_active2")
    practice, _user = await seed_practice(
        db_session,
        name="Active Test Practice 2",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )
    started = datetime.now(UTC) - timedelta(seconds=30)
    retell_id = _uid("ret_active2")
    call = await _seed_call(
        db_session,
        practice,
        retell_call_id=retell_id,
        status="in_progress",
        started_at=started,
    )

    resp = await client.get(
        "/api/calls/active",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    summary = body["active_calls"][0]
    assert summary["id"] == str(call.id)
    assert summary["retell_call_id"] == retell_id
    assert summary["direction"] == "inbound"
    assert summary["from_number"] == "+15551112222"
    # Duration should be > 0 (call started 30 seconds ago).
    assert summary["duration_seconds_so_far"] > 0


async def test_active_calls_rls_practice_isolation(client, db_session):
    """Practice A's active call must not appear in Practice B's results."""
    org_a = _uid("org_activeA")
    user_a = _uid("user_activeA")
    org_b = _uid("org_activeB")
    user_b = _uid("user_activeB")
    practice_a, _user_a = await seed_practice(
        db_session,
        name="Practice A Active",
        clerk_org_id=org_a,
        clerk_user_id=user_a,
    )
    practice_b, _user_b = await seed_practice(
        db_session,
        name="Practice B Active",
        clerk_org_id=org_b,
        clerk_user_id=user_b,
    )

    retell_a = _uid("ret_A_active")
    retell_b = _uid("ret_B_active")
    await _seed_call(
        db_session, practice_a, retell_call_id=retell_a, status="in_progress"
    )
    await _seed_call(
        db_session, practice_b, retell_call_id=retell_b, status="in_progress"
    )

    # Practice B should only see its own call.
    resp = await client.get(
        "/api/calls/active",
        headers={"X-Dev-Clerk-User-Id": user_b, "X-Dev-Clerk-Org-Id": org_b},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["active_calls"][0]["retell_call_id"] == retell_b

    # Practice A should only see its own call.
    resp_a = await client.get(
        "/api/calls/active",
        headers={"X-Dev-Clerk-User-Id": user_a, "X-Dev-Clerk-Org-Id": org_a},
    )
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["count"] == 1
    assert body_a["active_calls"][0]["retell_call_id"] == retell_a
