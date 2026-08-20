"""Why is this clinic like this — answerable from its own card.

The audit list answers "what happened lately" across everything. The question
somebody actually has, opening a clinic that is behaving strangely, is "who
approved it, who changed its plan, who connected its PMS" — and answering that
from a 500-row global list meant reading every line looking for one uuid.
"""

from __future__ import annotations

from tests.conftest import seed_practice
from tests.test_routes.test_admin import _h, _internal


async def test_the_history_can_be_narrowed_to_one_clinic(client, db_session):
    await _internal(db_session, clerk_id="sa_hist", role="super_admin")
    one, _ = await seed_practice(
        db_session, name="Watched", clerk_org_id="org_h1", clerk_user_id="u_h1"
    )
    other, _ = await seed_practice(
        db_session, name="Unrelated", clerk_org_id="org_h2", clerk_user_id="u_h2"
    )

    # Two real admin actions, one per clinic — each writes its own audit row.
    for practice in (one, other):
        r = await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sa_hist"))
        assert r.status_code == 200, r.text

    rows = (await client.get(
        f"/api/admin/audit-logs?practice_id={one.id}", headers=_h("sa_hist")
    )).json()
    assert rows, "a clinic that was just acted on has no history"
    assert {r["practice_id"] for r in rows} == {str(one.id)}, (
        "another clinic's actions appeared under this one's history"
    )


async def test_the_history_says_who_not_just_a_uuid(client, db_session):
    """A uuid answers "was it the same person twice" and nothing else. The
    question this list is opened for is "who approved this clinic"."""
    user = await _internal(db_session, clerk_id="sa_who", role="super_admin")
    practice, _ = await seed_practice(
        db_session, name="Named", clerk_org_id="org_h3", clerk_user_id="u_h3"
    )
    await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sa_who"))

    rows = (await client.get(
        f"/api/admin/audit-logs?practice_id={practice.id}", headers=_h("sa_who")
    )).json()
    assert rows[0]["actor"] == user.email
    assert rows[0]["user_id"], "the id is still there for machine matching"


async def test_the_global_list_still_works(client, db_session):
    """Without a filter it stays what it was — the whole picture."""
    await _internal(db_session, clerk_id="sa_all", role="super_admin")
    practice, _ = await seed_practice(
        db_session, name="Any", clerk_org_id="org_h4", clerk_user_id="u_h4"
    )
    await client.get(f"/api/admin/clinics/{practice.id}", headers=_h("sa_all"))

    r = await client.get("/api/admin/audit-logs", headers=_h("sa_all"))
    assert r.status_code == 200
    assert len(r.json()) >= 1
