"""_resolve_practice routing must never attribute a call to the wrong clinic.

Backward-compatible HIPAA fix: a call is routed to a practice by its Retell
agent_id; when that can't be resolved we fall back to the single practice only
when exactly one exists, and REFUSE (return None) when 2+ exist rather than
leaking one clinic's call into another's records.
"""

from __future__ import annotations

from app.webhooks.retell import _resolve_practice
from tests.conftest import seed_practice


async def test_matches_by_agent_id(db_session):
    p, _ = await seed_practice(
        db_session, name="A", clerk_org_id="org_ra", clerk_user_id="user_ra"
    )
    p.retell_agent_id = "agent_A"
    await db_session.commit()

    resolved = await _resolve_practice("agent_A")
    assert resolved is not None
    assert str(resolved.id) == str(p.id)


async def test_single_practice_fallback_when_no_agent_id(db_session):
    p, _ = await seed_practice(
        db_session, name="A", clerk_org_id="org_rb", clerk_user_id="user_rb"
    )
    # One practice, no agent_id → safe single-tenant fallback.
    resolved = await _resolve_practice(None)
    assert resolved is not None
    assert str(resolved.id) == str(p.id)


async def test_single_practice_fallback_when_agent_id_unmatched(db_session):
    p, _ = await seed_practice(
        db_session, name="A", clerk_org_id="org_rb2", clerk_user_id="user_rb2"
    )
    # One practice, unmatched agent_id → still safe (only one tenant possible).
    resolved = await _resolve_practice("agent_does_not_exist")
    assert resolved is not None
    assert str(resolved.id) == str(p.id)


async def test_refuses_when_ambiguous(db_session):
    await seed_practice(
        db_session, name="A", clerk_org_id="org_rc1", clerk_user_id="user_rc1"
    )
    await seed_practice(
        db_session, name="B", clerk_org_id="org_rc2", clerk_user_id="user_rc2"
    )
    # Two practices + unmatched agent_id → refuse, do NOT leak.
    resolved = await _resolve_practice("agent_unknown")
    assert resolved is None


async def test_matches_correct_practice_among_many(db_session):
    await seed_practice(
        db_session, name="A", clerk_org_id="org_rd1", clerk_user_id="user_rd1"
    )
    b, _ = await seed_practice(
        db_session, name="B", clerk_org_id="org_rd2", clerk_user_id="user_rd2"
    )
    b.retell_agent_id = "agent_B"
    await db_session.commit()

    resolved = await _resolve_practice("agent_B")
    assert resolved is not None
    assert str(resolved.id) == str(b.id)
