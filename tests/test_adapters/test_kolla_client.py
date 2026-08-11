"""Kolla computes availability by subtraction, and that is the whole risk.

NexHealth answers "what times are free?" directly. Kolla answers two narrower
questions — when is this room open, and what is already booked in it — and
expects us to do the arithmetic. Every way that arithmetic can be wrong shows up
as a patient being offered a time the clinic cannot honour, which is worse than
offering nothing.

These run against a fake transport. The shapes come from Kolla's published
reference; Eaglesoft has to be enabled per account by their support and ours is
not enabled yet, so nothing here has been seen from a live connector. That is
exactly why the arithmetic is tested now: it is the part that does not depend on
their answer.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.kolla.client import KollaClient, KollaError, KollaUnavailable

_ROOM = "resources/op-1"


def _client(routes: dict, **kwargs) -> KollaClient:
    """routes: path fragment → (status, payload) or a callable(request)."""

    def handle(request: httpx.Request) -> httpx.Response:
        for fragment, result in routes.items():
            if fragment in str(request.url):
                if callable(result):
                    return result(request)
                status, payload = result
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"message": f"no route for {request.url}"})

    return KollaClient(
        api_key="k", consumer_id="consumers/1",
        transport=httpx.MockTransport(handle), **kwargs
    )


def _schedule(*blocks) -> dict:
    return {"schedule": [{"date": "2026-09-01",
                          "blocks": [{"start_time": s, "end_time": e} for s, e in blocks]}]}


def _appointments(*spans) -> dict:
    return {"appointments": [
        {"start_time": s, "end_time": e, "operatory": _ROOM} for s, e in spans
    ]}


async def test_open_hours_become_slots_of_the_requested_length():
    client = _client({
        ":loadSchedule": (200, _schedule(("09:00", "11:00"))),
        "/appointments": (200, _appointments()),
    })
    slots = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    )
    assert [s.start_time[11:16] for s in slots] == ["09:00", "10:00"]
    assert all(s.operatory_id == _ROOM for s in slots)


async def test_a_booked_appointment_removes_the_time_it_occupies():
    client = _client({
        ":loadSchedule": (200, _schedule(("09:00", "12:00"))),
        "/appointments": (200, _appointments(
            ("2026-09-01T10:00:00Z", "2026-09-01T11:00:00Z"))),
    })
    slots = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    )
    assert [s.start_time[11:16] for s in slots] == ["09:00", "11:00"]


async def test_an_appointment_that_merely_overlaps_still_blocks_the_slot():
    """A 09:30 appointment starts nowhere near our grid, and an equality check
    would happily offer 09:00 — sending a patient into the middle of somebody
    else's cleaning."""
    client = _client({
        ":loadSchedule": (200, _schedule(("09:00", "12:00"))),
        "/appointments": (200, _appointments(
            ("2026-09-01T09:30:00Z", "2026-09-01T10:15:00Z"))),
    })
    slots = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    )
    # 09:00 and 10:00 both touch the booking; 11:00 is clear.
    assert [s.start_time[11:16] for s in slots] == ["11:00"]


async def test_offers_sit_on_a_grid_and_a_short_gap_is_not_offered():
    """Open 09:00-11:00 with a 09:30-10:15 booking leaves 30 minutes and 45
    minutes free. Neither fits an hour, so the honest answer is nothing.

    Slots are counted from the start of each open block rather than from the end
    of the last appointment. A clinic books on a grid, and an offer of "10:15,
    if you can be quick" is not something a front desk wants to defend.
    """
    client = _client({
        ":loadSchedule": (200, _schedule(("09:00", "11:00"))),
        "/appointments": (200, _appointments(
            ("2026-09-01T09:30:00Z", "2026-09-01T10:15:00Z"))),
    })
    assert await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    ) == []
    # The same day with a 30-minute appointment length does have room.
    shorter = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=30, resource_ids=[_ROOM]
    )
    assert [s.start_time[11:16] for s in shorter] == ["09:00", "10:30"]


async def test_a_slot_never_runs_past_closing():
    """Open 09:00–10:30 with hour slots is one slot, not two. The second would
    end at 11:00, half an hour after the room is dark."""
    client = _client({
        ":loadSchedule": (200, _schedule(("09:00", "10:30"))),
        "/appointments": (200, _appointments()),
    })
    slots = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    )
    assert [s.start_time[11:16] for s in slots] == ["09:00"]


async def test_a_cancelled_appointment_gives_its_time_back():
    """Cancellations are how a full day becomes bookable again. Counting them as
    busy hides real openings from every caller for the rest of the day."""
    def handle(request: httpx.Request) -> httpx.Response:
        if ":loadSchedule" in str(request.url):
            return httpx.Response(200, json=_schedule(("09:00", "10:00")))
        return httpx.Response(200, json={"appointments": [{
            "start_time": "2026-09-01T09:00:00Z", "end_time": "2026-09-01T10:00:00Z",
            "operatory": _ROOM, "cancelled": True,
        }]})

    client = KollaClient(api_key="k", consumer_id="consumers/1",
                         transport=httpx.MockTransport(handle))
    slots = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    )
    assert [s.start_time[11:16] for s in slots] == ["09:00"]


async def test_a_malformed_block_is_dropped_rather_than_guessed():
    """A block we cannot parse becomes an appointment the clinic cannot honour
    if we invent a reading for it."""
    client = _client({
        ":loadSchedule": (200, {"schedule": [{"date": "2026-09-01", "blocks": [
            {"start_time": "nonsense", "end_time": "10:00"},
            {"start_time": "14:00", "end_time": "15:00"},
        ]}]}),
        "/appointments": (200, _appointments()),
    })
    slots = await client.find_appointment_slots(
        start_date="2026-09-01", slot_length=60, resource_ids=[_ROOM]
    )
    assert [s.start_time[11:16] for s in slots] == ["14:00"]


async def test_the_practice_is_named_on_every_request():
    """Without consumer-id a request spans every practice on the connector. On a
    patient call that is another clinic's schedule."""
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if ":loadSchedule" in str(request.url):
            return httpx.Response(200, json=_schedule(("09:00", "10:00")))
        return httpx.Response(200, json=_appointments())

    client = KollaClient(api_key="k", consumer_id="consumers/42",
                         transport=httpx.MockTransport(handle))
    await client.find_appointment_slots(
        start_date="2026-09-01", resource_ids=[_ROOM]
    )
    assert seen and all(h.get("consumer-id") == "consumers/42" for h in seen)
    assert all(h.get("authorization") == "Bearer k" for h in seen)


async def test_no_credentials_is_an_error_not_an_empty_calendar():
    client = KollaClient(api_key="", consumer_id="consumers/1")
    with pytest.raises(KollaError):
        await client.find_appointment_slots(start_date="2026-09-01", resource_ids=[_ROOM])


async def test_their_outage_is_distinct_from_their_rejection():
    """The caller falls back to our own book on an outage. A 400 is our bug and
    must not be disguised as one."""
    down = _client({"": (503, {"message": "down"})})
    with pytest.raises(KollaUnavailable):
        await down.find_appointment_slots(start_date="2026-09-01", resource_ids=[_ROOM])

    rejected = _client({"": (400, {"message": "bad filter"})})
    with pytest.raises(KollaError):
        await rejected.find_appointment_slots(start_date="2026-09-01", resource_ids=[_ROOM])


async def test_writing_an_appointment_sends_the_shape_kolla_documents():
    sent: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={
            "name": "appointments/77", "remote_id": "ES-77",
            "start_time": "2026-09-01T09:00:00Z",
        })

    client = KollaClient(api_key="k", consumer_id="consumers/1",
                         transport=httpx.MockTransport(handle))
    created = await client.create_appointment(
        patient_pms_id="123", start_time="2026-09-01T09:00:00Z",
        end_time="2026-09-01T10:00:00Z", operatory_id=_ROOM, note="cleaning",
    )
    # A bare patient id is normalised — Kolla addresses patients by resource name.
    assert sent["contact_id"] == "contacts/123"
    assert sent["operatory"] == _ROOM
    # Kolla's own resource name, NOT remote_id. remote_id is the id inside
    # Eaglesoft: it reads well in a log and addresses nothing here. Storing it
    # would have made every appointment we create impossible to cancel or move
    # afterwards, found the first time a patient rang back to cancel one.
    assert created.appointment_id == "appointments/77"


async def test_a_write_with_no_usable_id_is_a_failure():
    """Storing an empty PMS id would make the booking look synced while nothing
    in the clinic's calendar points back to it."""
    client = _client({"/appointments": (200, {"start_time": "2026-09-01T09:00:00Z"})})
    with pytest.raises(KollaError):
        await client.create_appointment(
            patient_pms_id="1", start_time="2026-09-01T09:00:00Z",
            end_time="2026-09-01T10:00:00Z",
        )


async def test_cancelling_and_moving_address_the_appointment_by_name():
    """Both take "appointments/{id}", and both accept a bare id so anything
    stored before that was true still resolves."""
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"name": "appointments/77", "cancelled": True})

    client = KollaClient(api_key="k", consumer_id="consumers/1",
                         transport=httpx.MockTransport(handle))
    await client.cancel_appointment("appointments/77")
    await client.move_appointment(
        "77", start_time="2026-09-02T09:00:00Z", end_time="2026-09-02T10:00:00Z"
    )
    assert seen[0] == ("POST", "/dental/v1/appointments/77:cancel")
    assert seen[1] == ("PATCH", "/dental/v1/appointments/77")


async def test_a_move_names_the_fields_it_changes():
    """Without update_mask, Kolla applies every field in the body — and a body
    carrying only the times would blank whatever it omitted."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["query"] = str(request.url.query)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"name": "appointments/77"})

    client = KollaClient(api_key="k", consumer_id="consumers/1",
                         transport=httpx.MockTransport(handle))
    await client.move_appointment(
        "appointments/77",
        start_time="2026-09-02T09:00:00Z", end_time="2026-09-02T10:00:00Z",
    )
    assert "update_mask=start_time%2Cend_time" in seen["query"]
    assert set(seen["body"]) == {"start_time", "end_time"}
