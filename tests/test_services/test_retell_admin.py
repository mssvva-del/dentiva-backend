"""Publishing an agent has to reach the numbers, or it reaches nobody."""

import json

import httpx
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    """conftest blanks production credentials; these tests never leave the
    MockTransport, so any non-empty value is enough to get past the guard."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "retell_api_key", "test-key")

async def test_publishing_moves_every_number_to_the_new_version():
    """A Retell number pins a version; publishing does not move the pin. With one
    number per clinic, the clinics that signed up earliest would keep hearing the
    old agent after a fix ships — silently, while every screen shows the new one."""
    from app.services.retell_admin import repin_numbers_to_published

    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/list-phone-numbers":
            return httpx.Response(200, json=[
                {"phone_number": "+15551110000",
                 "inbound_agents": [{"agent_id": "agent_ours", "agent_version": 12}]},
                {"phone_number": "+15552220000",
                 "inbound_agents": [{"agent_id": "agent_someone_else"}]},
                {"phone_number": "+15553330000", "inbound_agents": []},
            ])
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    repinned = await repin_numbers_to_published("agent_ours", transport=transport)

    assert repinned == ["+15551110000"]
    # Only our agent's number is touched, and no version is sent — omitting it is
    # what makes Retell bind to current published.
    assert len(seen) == 1
    method, path, body = seen[0]
    assert method == "PATCH"
    assert path == "/update-phone-number/+15551110000"
    # No agent VERSION — omitting it is what makes Retell bind to current
    # published. The weight is required by their API on every binding.
    assert body == {"inbound_agents": [{"agent_id": "agent_ours", "weight": 1}]}


async def test_one_unreachable_number_does_not_strand_the_rest():
    """Collected, not raised: a single failing number must not leave every other
    clinic on the old version."""
    from app.services.retell_admin import repin_numbers_to_published

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/list-phone-numbers":
            return httpx.Response(200, json=[
                {"phone_number": "+15551110000",
                 "inbound_agents": [{"agent_id": "agent_ours"}]},
                {"phone_number": "+15559990000",
                 "inbound_agents": [{"agent_id": "agent_ours"}]},
            ])
        if "+15551110000" in request.url.path:
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={})

    repinned = await repin_numbers_to_published(
        "agent_ours", transport=httpx.MockTransport(handler)
    )
    assert repinned == ["+15559990000"]
