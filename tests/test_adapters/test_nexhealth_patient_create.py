"""Getting a caller into the clinic's own system.

Until now a first-time caller existed only in OUR database. The agent took the
appointment, the practice's system had never heard of the person, and the front
desk had to type them in from an SMS or the slot existed nowhere real.

Every shape here was verified against the live sandbox on 2026-08-13, and each
one contradicted the published documentation. That is the point of the file: the
docs describe an API that rejects what they describe.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.nexhealth.client import (
    NexHealthClient,
    NexHealthDuplicate,
    NexHealthError,
)


def _client(handler) -> NexHealthClient:
    return NexHealthClient(
        api_key="k", subdomain="sub", location_id="1",
        transport=httpx.MockTransport(handler),
    )


def _auth(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/authenticates":
        return httpx.Response(201, json={"data": {"token": "t"}})
    return None


async def test_the_provider_goes_in_the_body(monkeypatch):
    """The documented ``?provider_id=`` query form answers 400 "Missing parameter
    provider[provider_id]". Verified, not assumed."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        seen["query"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"data": {"id": 514156368}})

    pms_id = await _client(handler).create_patient(
        first_name="Ann", last_name="Lee", phone="+1 (620) 555-1111",
        date_of_birth="1984-03-02", provider_id="495693793",
        email="no-reply@no-reply.dentovox.com",
    )
    assert pms_id == "514156368"
    assert seen["body"]["provider"] == {"provider_id": "495693793"}
    assert "provider_id" not in seen["query"]
    # Digits only: the PMS is fussier about the number than about the person.
    assert seen["body"]["patient"]["bio"]["phone_number"] == "6205551111"
    assert seen["body"]["patient"]["bio"]["date_of_birth"] == "1984-03-02"


async def test_a_patient_who_already_exists_is_adopted_not_lost():
    """NexHealth de-duplicates and puts the id in the ERROR body. Treating that
    as a failure would drop a booking for a patient who is demonstrably there —
    the one case where the API is telling us we already won."""
    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        return httpx.Response(400, json={
            "error": ["A patient with that information already exists - id=514156368"]
        })

    pms_id = await _client(handler).create_patient(
        first_name="Ann", last_name="Lee", phone="+16205551111",
        date_of_birth="1984-03-02", provider_id="1", email="x@no-reply.dentovox.com",
    )
    assert pms_id == "514156368"


async def test_any_other_rejection_still_raises():
    """The duplicate is the only 400 that means success. A missing field must not
    quietly produce a patient id of nothing."""
    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        return httpx.Response(400, json={
            "error": ["Missing parameter patient[bio][date_of_birth]"]
        })

    with pytest.raises(NexHealthError) as caught:
        await _client(handler).create_patient(
            first_name="Ann", last_name="Lee", phone="+16205551111",
            date_of_birth="", provider_id="1", email="x@no-reply.dentovox.com",
        )
    assert not isinstance(caught.value, NexHealthDuplicate)


async def test_an_error_body_never_reaches_the_message():
    """Error text echoes what we sent, and for this endpoint that is a real
    person's name, number and date of birth. Only the duplicate id survives."""
    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        return httpx.Response(422, json={
            "error": ["Invalid patient Ann Lee 6205551111 1984-03-02"]
        })

    with pytest.raises(NexHealthError) as caught:
        await _client(handler).create_patient(
            first_name="Ann", last_name="Lee", phone="+16205551111",
            date_of_birth="1984-03-02", provider_id="1", email="x@no-reply.dentovox.com",
        )
    message = str(caught.value)
    for leaked in ("Ann", "Lee", "6205551111", "1984-03-02"):
        assert leaked not in message, f"{leaked!r} leaked into an exception message"


async def test_a_returning_caller_is_found_by_their_number():
    """This is what keeps most calls short: someone already in the practice's
    system needs no date of birth and no questions."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": {"patients": [
            {"id": 1, "last_name": "Other", "bio": {"phone_number": "9999999999"}},
            {"id": 42, "last_name": "Lee", "bio": {"phone_number": "(620) 555-1111"}},
        ]}})

    found = await _client(handler).find_patient_id(phone="+16205551111")
    assert found == "42"
    # The parameter is phone_number. "search" is silently IGNORED — it answered
    # 200 with the first five patients in the practice, none of them the one
    # being looked for.
    assert seen["params"]["phone_number"] == "6205551111"


async def test_a_lookup_that_returns_strangers_matches_nobody():
    """The failure mode the sandbox actually showed: a search parameter the API
    ignores, answering 200 with whoever happens to be first. Adopting one of
    those would put a stranger's id on this caller's record and book the
    appointment into someone else's chart."""
    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        return httpx.Response(200, json={"data": {"patients": [
            {"id": 7, "last_name": "Nobody", "bio": {"phone_number": "2125550000"}},
            {"id": 8, "last_name": "Else", "bio": {"phone_number": "3105550000"}},
        ]}})

    assert await _client(handler).find_patient_id(phone="+16205551111") is None


async def test_a_lookup_failure_is_not_a_booking_failure():
    """Not being able to look someone up is not a reason to lose a booking: the
    caller falls through to creating them, and NexHealth refuses a duplicate."""
    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        return httpx.Response(500)

    assert await _client(handler).find_patient_id(phone="+16205551111") is None


async def test_an_inactive_provider_is_not_offered():
    """A patient attached to a retired dentist is a record somebody has to fix."""
    def handler(request: httpx.Request) -> httpx.Response:
        if (resp := _auth(request)) is not None:
            return resp
        return httpx.Response(200, json={"data": [
            {"id": 1, "inactive": True},
            {"id": 2, "inactive": False},
        ]})

    assert await _client(handler).first_provider_id() == "2"
