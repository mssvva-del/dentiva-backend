"""A page that never left must not be silent.

The agent tells a caller with bleeding or swelling that the clinic has been
notified and will call them straight back. The only thing behind that sentence is
one SMS — the dashboard row is not a page, and the code says so itself.

send_sms returns {"skipped": ...} when SMS is switched off or Twilio is not
configured, logs at INFO, and alerts nobody. Nothing looked at the result. So the
entire notification path could be inert — an expired credential, a typo in an env
var, a clinic that never had SMS enabled — and every call would still sound
perfect, the callback row would sit in a dashboard nobody has open, and the first
sign would be the patient.

These tests are about the noise, not the send. Whether Twilio delivers is
Twilio's business; whether we NOTICE that it did not is ours.
"""

from __future__ import annotations

import asyncio

from app.observability import alerts
from tests.conftest import seed_practice


async def _drain_background_sends() -> None:
    """The send is detached on purpose — Twilio can take fifteen seconds and that
    is dead air on a live call. Let the loop run it before asserting."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_an_undelivered_urgent_page_raises_an_alert(client, db_session, monkeypatch):
    """SMS switched off is the ordinary way this happens, and it is exactly the
    configuration that looks like nothing is wrong."""
    from app.services import sms as sms_service

    practice, _ = await seed_practice(
        db_session, name="Page Dental", clerk_org_id="org_pg1", clerk_user_id="user_pg1"
    )
    practice.transfer_phone_number = "+15557778888"
    await db_session.commit()

    # Positional, because send_sms is called as send_sms(to, body). A stub that
    # only takes keywords raises TypeError inside the detached task, the task is
    # never awaited, and the failure is swallowed — which is the very shape of
    # bug this file is about, reproduced in its own scaffolding.
    async def _switched_off(to, body, **kwargs):
        return {"skipped": "sms_disabled"}

    monkeypatch.setattr(sms_service, "send_sms", _switched_off)
    alerts._RECENT.clear()

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "page-1",
        "call": {"from_number": "+15551110000", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "page-1",
        "function_name": "create_callback_request",
        "args": {"patient_name": "Ann", "patient_phone": "+15551110000",
                 "reason": "bleeding since the extraction", "urgent": True},
    })
    assert r.status_code == 200
    await _drain_background_sends()

    fired = alerts.recent_alerts()["by_kind"]
    assert any(k.startswith("page_not_delivered") for k in fired), (
        f"the clinic was never paged and nothing said so: {fired}"
    )


async def test_a_delivered_page_is_quiet(client, db_session, monkeypatch):
    """An alert on every successful page would be worse than none — the one that
    matters would arrive in a crowd."""
    from app.services import sms as sms_service

    practice, _ = await seed_practice(
        db_session, name="Page Dental 2", clerk_org_id="org_pg2", clerk_user_id="user_pg2"
    )
    practice.transfer_phone_number = "+15557778888"
    await db_session.commit()

    async def _sent(to, body, **kwargs):
        return {"sid": "SM123"}

    monkeypatch.setattr(sms_service, "send_sms", _sent)
    alerts._RECENT.clear()

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "page-2",
        "call": {"from_number": "+15551110000", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "page-2",
        "function_name": "create_callback_request",
        "args": {"patient_name": "Ann", "patient_phone": "+15551110000",
                 "reason": "toothache", "urgent": True},
    })
    await _drain_background_sends()

    assert not any(
        k.startswith("page_not_delivered")
        for k in alerts.recent_alerts()["by_kind"]
    )
