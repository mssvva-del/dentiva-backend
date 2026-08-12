"""The ROI card is the number a practice decides by.

It is the first thing on the analytics page and the sentence an owner repeats
when someone asks whether the AI is worth it. Which makes a flattering error
worse than a missing figure: nobody argues with a number that agrees with them.

The card counted every call that was not still ringing as "handled by AI" —
including the ones the AI missed. So the failures were displayed as successes,
and then counted a second time in the denominator of the answer rate, pushing
that up too. Both errors pointed the same way: away from our own failures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.call import Call
from tests.conftest import seed_practice


async def _call(db_session, practice_id, *, status: str, days_ago: int = 1):
    db_session.add(Call(
        id=uuid.uuid4(), practice_id=practice_id,
        retell_call_id=f"roi-{uuid.uuid4().hex[:8]}",
        direction="inbound", from_number="+15551230000", to_number="+15559876543",
        started_at=datetime.now(UTC) - timedelta(days=days_ago),
        status=status, duration_seconds=120,
    ))
    await db_session.commit()


async def _roi(client, org_id, user_id):
    resp = await client.get(
        "/api/dashboard/roi",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_missed_call_is_not_a_handled_call(client, db_session):
    """THE test. Eight answered, two dropped, and the practice was told ten were
    handled by the AI receptionist that dropped two of them."""
    practice, _ = await seed_practice(
        db_session, name="ROI Dental", clerk_org_id="org_roi1", clerk_user_id="user_roi1"
    )
    for _ in range(8):
        await _call(db_session, practice.id, status="completed")
    for _ in range(2):
        await _call(db_session, practice.id, status="missed")

    data = await _roi(client, "org_roi1", "user_roi1")
    assert data["calls_handled_by_ai"] == 8
    assert data["calls_missed"] == 2


async def test_the_answer_rate_counts_each_call_once(client, db_session):
    """Missed calls were in both terms of the denominator and in the numerator.
    Eight of ten answered reads as 80%, not 83.3%."""
    practice, _ = await seed_practice(
        db_session, name="Rate Dental", clerk_org_id="org_roi2", clerk_user_id="user_roi2"
    )
    for _ in range(8):
        await _call(db_session, practice.id, status="completed")
    for _ in range(2):
        await _call(db_session, practice.id, status="missed")

    assert (await _roi(client, "org_roi2", "user_roi2"))["ai_answer_rate_pct"] == 80.0


async def test_a_practice_that_missed_everything_is_told_so(client, db_session):
    """The worst case, and the one the old arithmetic softened most: every call
    dropped used to read as 50% answered."""
    practice, _ = await seed_practice(
        db_session, name="Silent Dental", clerk_org_id="org_roi3", clerk_user_id="user_roi3"
    )
    for _ in range(5):
        await _call(db_session, practice.id, status="missed")

    data = await _roi(client, "org_roi3", "user_roi3")
    assert data["ai_answer_rate_pct"] == 0.0
    assert data["calls_handled_by_ai"] == 0


async def test_no_calls_at_all_does_not_divide_by_zero(client, db_session):
    """A clinic on its first day. 0% is honest here; the point is that it
    answers rather than raising."""
    await seed_practice(
        db_session, name="New Dental", clerk_org_id="org_roi4", clerk_user_id="user_roi4"
    )
    assert (await _roi(client, "org_roi4", "user_roi4"))["ai_answer_rate_pct"] == 0.0


async def test_no_invented_revenue_figure_is_published(client, db_session):
    """$150 a booking was a guess with no procedure and no fee schedule behind
    it. Nothing displays it — and a dollar amount an owner would repeat to their
    accountant should not sit in an API waiting for somebody to put it on a
    card."""
    practice, _ = await seed_practice(
        db_session, name="Money Dental", clerk_org_id="org_roi5", clerk_user_id="user_roi5"
    )
    await _call(db_session, practice.id, status="completed")
    assert (await _roi(client, "org_roi5", "user_roi5"))["revenue_protected_usd"] == 0.0
