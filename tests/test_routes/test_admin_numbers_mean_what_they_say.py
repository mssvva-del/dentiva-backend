"""The admin figures are the ones that get quoted to other people.

A dashboard that is merely incomplete is a nuisance. A dashboard that is
confidently wrong is worse, because somebody repeats the number in a meeting.
Both problems were on this page.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models.practice import Practice
from app.models.usage_record import UsageRecord
from tests.conftest import seed_practice
from tests.test_routes.test_admin import _h, _internal


async def _usage(db_session, practice_id, *, minutes, period_start):
    db_session.add(UsageRecord(
        id=uuid.uuid4(), practice_id=practice_id,
        period_start=period_start, period_end=period_start + timedelta(days=30),
        minutes_used=Decimal(minutes), calls_count=1,
    ))
    await db_session.commit()


async def test_period_minutes_is_this_period_not_all_of_history(
    client, db_session
):
    """It summed every usage row ever written while calling itself
    period_minutes — a number that can only grow, presented as a monthly one.
    Cost and margin were computed from it, so both drifted further from the truth
    with every call the product had ever taken."""
    await _internal(db_session, clerk_id="sa_numbers", role="super_admin")
    practice, _ = await seed_practice(
        db_session, name="Usage Co", clerk_org_id="org_u1", clerk_user_id="user_u1"
    )
    now = datetime.now(tz=UTC)
    await _usage(db_session, practice.id, minutes=1000, period_start=now - timedelta(days=400))
    await _usage(db_session, practice.id, minutes=40, period_start=now.replace(day=1))

    r = await client.get("/api/admin/revenue", headers=_h("sa_numbers"))
    assert r.status_code == 200, r.text
    assert r.json()["period_minutes"] == 40, "last year's minutes are in this month's total"


async def test_the_canary_is_not_counted_as_a_clinic(client, db_session):
    """A monitoring tenant in the clinic count is a number somebody reports."""
    await _internal(db_session, clerk_id="sa_numbers", role="super_admin")
    await seed_practice(
        db_session, name="Real Co", clerk_org_id="org_u2", clerk_user_id="user_u2"
    )
    canary, _ = await seed_practice(
        db_session, name="Dentovox Monitoring",
        clerk_org_id="org_u3", clerk_user_id="user_u3",
    )
    canary.is_canary = True
    canary.status = "active"
    await db_session.commit()

    body = (await client.get("/api/admin/revenue", headers=_h("sa_numbers"))).json()
    real = (await db_session.execute(
        select(Practice).where(Practice.is_canary.is_(False), Practice.status == "active")
    )).scalars().all()
    assert body["active_clinics"] == len(real)


async def test_a_clinic_shows_what_it_used_against_what_it_pays_for(
    client, db_session
):
    """Lifetime counts say a clinic has been busy. They cannot say whether it is
    about to cost us money — a practice quietly running at three times its bucket
    looks identical to a happy one until the invoice arrives."""
    await _internal(db_session, clerk_id="sa_numbers", role="super_admin")
    practice, _ = await seed_practice(
        db_session, name="Heavy Co", clerk_org_id="org_u4", clerk_user_id="user_u4"
    )
    now = datetime.now(tz=UTC)
    await _usage(db_session, practice.id, minutes=850, period_start=now.replace(day=1))
    await _usage(db_session, practice.id, minutes=9999, period_start=now - timedelta(days=400))

    body = (await client.get(
        f"/api/admin/clinics/{practice.id}", headers=_h("sa_numbers")
    )).json()
    assert body["period_minutes_used"] == 850, "history leaked into this period"
    assert "period_minutes_included" in body


async def test_the_clinic_list_carries_usage_without_a_query_per_clinic(
    client, db_session
):
    """The column exists so the list answers "who is worth looking at" without
    opening every clinic. It has to come from one grouped query — at seven
    practices the difference is invisible and at seventy it is the page."""
    await _internal(db_session, clerk_id="sa_list", role="super_admin")
    now = datetime.now(tz=UTC)
    for name, minutes in (("A Co", 120), ("B Co", 3400), ("C Co", 0)):
        practice, _ = await seed_practice(
            db_session, name=name,
            clerk_org_id=f"org_{name[0]}", clerk_user_id=f"user_{name[0]}",
        )
        if minutes:
            await _usage(db_session, practice.id, minutes=minutes,
                         period_start=now.replace(day=1))
            # Last year's usage must not appear in this month's column.
            await _usage(db_session, practice.id, minutes=50_000,
                         period_start=now - timedelta(days=400))

    rows = (await client.get("/api/admin/clinics", headers=_h("sa_list"))).json()
    by_name = {r["name"]: r for r in rows}
    assert by_name["A Co"]["period_minutes_used"] == 120
    assert by_name["B Co"]["period_minutes_used"] == 3400
    assert by_name["C Co"]["period_minutes_used"] == 0
    assert all("period_minutes_included" in r for r in rows)


async def test_the_list_says_when_each_clinic_last_had_a_call(client, db_session):
    """Silence is the failure nobody notices. If the number breaks or the
    practice turns forwarding off, calls stop and every other column stays
    exactly as it was — the money is still billed, the plan is still there, the
    dashboard is still green.

    The practice finds out before we do, and reports it as "your product does not
    work" rather than "our forwarding is off", because from their side those are
    the same sentence.
    """
    from app.db import set_tenant
    from app.models.call import Call

    await _internal(db_session, clerk_id="sa_quiet", role="super_admin")
    loud, _ = await seed_practice(
        db_session, name="Loud Co", clerk_org_id="org_q1", clerk_user_id="user_q1"
    )
    await seed_practice(
        db_session, name="Silent Co", clerk_org_id="org_q2", clerk_user_id="user_q2"
    )
    when = datetime.now(tz=UTC) - timedelta(hours=3)
    await set_tenant(db_session, loud.id)
    db_session.add(Call(
        id=uuid.uuid4(), practice_id=loud.id, retell_call_id="quiet-1",
        direction="inbound", from_number="+15551110000", to_number="+15559876543",
        started_at=when, status="completed",
    ))
    await db_session.commit()

    rows = {r["name"]: r for r in
            (await client.get("/api/admin/clinics", headers=_h("sa_quiet"))).json()}
    assert rows["Loud Co"]["last_call_at"] is not None
    assert rows["Silent Co"]["last_call_at"] is None, (
        "a clinic that has never had a call must be visibly different"
    )
