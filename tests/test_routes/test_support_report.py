"""One button on a broken screen, and we hear about it with the log thread.

The request id has been stamped on every request and echoed on every response
since the middleware was written — "so a caller can quote it in a bug report",
per its own docstring. Nothing ever showed it to the caller, so reports arrived
as "the dashboard isn't loading", which matches every log line and none.
"""

from __future__ import annotations

from app.observability import alerts
from tests.conftest import seed_practice


async def test_a_report_lands_in_the_alert_stream(client, db_session):
    await seed_practice(
        db_session, name="Report Dental", clerk_org_id="org_rep1", clerk_user_id="u_rep1"
    )
    alerts._RECENT.clear()

    r = await client.post(
        "/api/support/report",
        headers={"X-Dev-Clerk-User-Id": "u_rep1", "X-Dev-Clerk-Org-Id": "org_rep1"},
        json={"request_id": "abc123def456", "screen": "/calls", "status_code": 500},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"received": True, "reference": "abc123def456"}

    fired = alerts.recent_alerts()
    assert "clinic_reported_problem" in fired["by_kind"]
    # The detail carries the thread to pull — and nothing a patient said.
    assert "abc123def456" in fired["last_detail"]
    assert "/calls" in fired["last_detail"]


async def test_a_report_without_a_reference_still_lands(client, db_session):
    """A screen that failed to render at all has no response to read an id from.
    The report is still worth having — practice + screen narrows the logs."""
    await seed_practice(
        db_session, name="Report Dental 2", clerk_org_id="org_rep2", clerk_user_id="u_rep2"
    )
    alerts._RECENT.clear()

    r = await client.post(
        "/api/support/report",
        headers={"X-Dev-Clerk-User-Id": "u_rep2", "X-Dev-Clerk-Org-Id": "org_rep2"},
        json={"screen": "/bookings"},
    )
    assert r.status_code == 200
    assert "clinic_reported_problem" in alerts.recent_alerts()["by_kind"]


async def test_an_unauthenticated_report_is_refused(client):
    """The endpoint writes into the stream that pages a human. Unauthenticated,
    it would be a free way to page us from the open internet."""
    r = await client.post("/api/support/report", json={"screen": "/calls"})
    assert r.status_code == 401
