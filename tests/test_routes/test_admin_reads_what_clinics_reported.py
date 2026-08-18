"""What a clinic told us was broken, and whether anyone dealt with it.

The Report button writes into the alert stream, which pages us — useful for the
first hour and useless for the second, because a stream has no memory of what
was handled. Without this screen the button is a way to make noise at ourselves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.alert_event import AlertEvent
from tests.conftest import seed_practice
from tests.test_routes.test_admin import _h, _internal

# alert_events is platform-level, not tenant-scoped, so rows survive between
# tests in this file. Each test therefore asserts on ITS OWN row rather than on
# the length of the list — an assertion that counts everything would pass or
# fail depending on which tests ran first.


async def _staff(db_session, who: str):
    """A fresh staff identity per test.

    users and alert_events are platform tables — they are not tenant-scoped and
    do not get cleaned between tests the way the per-clinic ones do. Sharing one
    clerk id across tests makes them pass or fail on the order they ran in.
    """
    await _internal(db_session, clerk_id=who, role="super_admin")


async def _report(db_session, detail: str, *, kind="clinic_reported_problem"):
    row = AlertEvent(
        id=uuid.uuid4(), kind=kind, detail=detail,
        created_at=datetime.now(tz=UTC),
    )
    db_session.add(row)
    await db_session.commit()
    return row.id


async def test_a_report_arrives_with_the_clinic_named(client, db_session):
    """The id alone means opening another screen to learn who is affected."""
    await _staff(db_session, "sa_rep0")
    practice, _ = await seed_practice(
        db_session, name="Riverside", clerk_org_id="org_rp1", clerk_user_id="u_rp1"
    )
    await _report(
        db_session,
        f"practice={practice.id} screen=/calls status=500 request_id=abc123",
    )

    r = await client.get("/api/admin/reports", headers=_h("sa_rep0"))
    assert r.status_code == 200, r.text
    mine = [x for x in r.json() if x["request_id"] == "abc123"]
    assert len(mine) == 1, r.json()
    assert mine[0]["practice_name"] == "Riverside"
    assert mine[0]["screen"] == "/calls"
    assert mine[0]["resolved_at"] is None


async def test_resolving_hides_it_and_says_who(client, db_session):
    await _staff(db_session, "sa_rep1")
    report_id = await _report(db_session, "practice=x screen=/bookings")

    done = await client.post(
        f"/api/admin/reports/{report_id}/resolve", headers=_h("sa_rep1")
    )
    assert done.status_code == 200, done.text
    assert done.json()["resolved_at"] is not None
    assert done.json()["resolved_by"]

    still_open = (await client.get("/api/admin/reports", headers=_h("sa_rep1"))).json()
    assert str(report_id) not in [x["id"] for x in still_open]

    with_resolved = (await client.get(
        "/api/admin/reports?include_resolved=true", headers=_h("sa_rep1")
    )).json()
    assert str(report_id) in [x["id"] for x in with_resolved]


async def test_resolving_twice_is_not_an_error(client, db_session):
    """Two operators clicking at once is the ordinary case, not a conflict —
    and the second click must not overwrite who actually handled it."""
    await _staff(db_session, "sa_rep2")
    report_id = await _report(db_session, "practice=x screen=/calls")

    first = await client.post(
        f"/api/admin/reports/{report_id}/resolve", headers=_h("sa_rep2")
    )
    second = await client.post(
        f"/api/admin/reports/{report_id}/resolve", headers=_h("sa_rep2")
    )
    assert second.status_code == 200
    assert second.json()["resolved_at"] == first.json()["resolved_at"]


async def test_an_unparseable_report_still_appears(client, db_session):
    """A detail that does not match our format is still a report. Dropping it
    would be the worst failure for a screen whose whole job is not losing them."""
    await _staff(db_session, "sa_rep3")
    await _report(db_session, "something nobody planned for")

    rows = (await client.get("/api/admin/reports", headers=_h("sa_rep3"))).json()
    mine = [x for x in rows if x["detail"] == "something nobody planned for"]
    assert len(mine) == 1
    assert mine[0]["screen"] is None


async def test_other_alert_kinds_are_not_in_this_list(client, db_session):
    """The stream carries pages about undelivered SMS and PMS failures too.
    Mixing them in would bury the ones a human was asked to look at."""
    await _staff(db_session, "sa_rep4")
    await _report(db_session, "practice=x", kind="page_not_delivered_urgent_callback")

    rows = (await client.get("/api/admin/reports", headers=_h("sa_rep4"))).json()
    assert all(x["kind"] == "clinic_reported_problem" for x in rows)
