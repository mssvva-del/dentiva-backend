"""A redeploy must not swallow a text that was already promised.

Every SMS is fired and forgotten, deliberately: Twilio can take fifteen seconds
and that is dead air on a live call. The cost is that shutting down mid-flight
drops it — the confirmation the caller was told to expect, or the page telling a
clinic that somebody is bleeding.

Railway redeploys on every merge. So this is not a rare window; it is one we open
several times a day, exactly as wide as the calls in flight.
"""

from __future__ import annotations

import asyncio

from app.webhooks.retell import _bg_sms_tasks, _fire_sms, drain_pending_sms


async def test_an_in_flight_text_is_waited_for():
    sent: list[str] = []

    async def _slow_send():
        await asyncio.sleep(0.05)
        sent.append("delivered")
        return {"sid": "SM1"}

    _bg_sms_tasks.clear()
    _fire_sms(_slow_send())
    assert sent == [], "the send should not have finished yet"

    waited = await drain_pending_sms(timeout=2.0)
    assert waited == 1
    assert sent == ["delivered"], "the text was dropped by shutdown"


async def test_a_stuck_send_does_not_hold_the_deploy_open():
    """Losing one text is better than a container that will not die — a deploy
    that hangs takes the whole service down, including the calls it was trying
    to be careful about."""
    async def _never_finishes():
        await asyncio.sleep(3600)

    _bg_sms_tasks.clear()
    _fire_sms(_never_finishes())

    waited = await asyncio.wait_for(drain_pending_sms(timeout=0.05), timeout=2.0)
    assert waited == 1


async def test_draining_nothing_is_free():
    """Shutdown runs on every redeploy; it must not pay for a queue that is
    usually empty."""
    _bg_sms_tasks.clear()
    assert await drain_pending_sms(timeout=5.0) == 0
