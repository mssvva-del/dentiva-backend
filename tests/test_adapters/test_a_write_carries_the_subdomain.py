"""A clinic whose credentials carry no subdomain.

NexHealth scopes every endpoint by subdomain and refuses a blank one outright.
Ours was resolved from /institutions on READS only, so such a clinic could be
asked about its calendar all day and never have anything written to it: the
agent booked, the write-back was refused, and the appointment lived in our book
and in no practice's software. The cancellation that finally surfaced it came
back as "Blank values not allowed for parameter subdomain" — three appointments
into a live clinic's morning.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.nexhealth.client import NexHealthClient

INSTITUTIONS = {"data": [{"subdomain": "harborside-dental"}]}


def _client(handler, **kw) -> NexHealthClient:
    return NexHealthClient(
        api_key="k", location_id="42", base_url="https://nex.test",
        transport=httpx.MockTransport(handler), **kw,
    )


def _routes(seen: dict):
    """A NexHealth that answers, and records the subdomain each call carried."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "t"}})
        if request.url.path == "/institutions":
            seen["institutions"] = seen.get("institutions", 0) + 1
            return httpx.Response(200, json=INSTITUTIONS)
        seen[request.method] = request.url.params.get("subdomain")
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"appt": {"id": 999}}})
        return httpx.Response(200, json={"data": {"appt": {"id": 999}}})

    return handler


@pytest.mark.asyncio
async def test_a_cancellation_carries_the_resolved_subdomain():
    seen: dict = {}
    await _client(_routes(seen)).cancel_appointment("1677173079")
    assert seen.get("PATCH") == "harborside-dental", seen


@pytest.mark.asyncio
async def test_a_booking_carries_the_resolved_subdomain():
    """The half that reached a live clinic: appointments in our book only."""
    seen: dict = {}
    await _client(_routes(seen)).create_appointment(
        patient_pms_id="1", provider_id="2", start_time="2099-10-05T13:00:00Z",
    )
    assert seen.get("POST") == "harborside-dental", seen


@pytest.mark.asyncio
async def test_a_clinic_that_gave_us_its_subdomain_is_never_asked():
    """One extra request per client is worth avoiding, and /institutions is the
    one endpoint that answers without a subdomain — so it must not become a
    prerequisite for clinics that already told us theirs."""
    seen: dict = {}
    await _client(_routes(seen), subdomain="told-us").cancel_appointment("1")
    assert seen.get("PATCH") == "told-us"
    assert "institutions" not in seen
