"""NexHealth reactivation pull — mock source + real client against mocked HTTP.

No network: NexHealth responses are stubbed with httpx.MockTransport so we verify
auth flow, pagination, dirty-data tolerance, and token refresh without live access.
"""

from __future__ import annotations

from datetime import date

import httpx

from app.adapters.nexhealth.client import NexHealthClient
from app.adapters.nexhealth.mock import MockReactivationSource


def _client(handler) -> NexHealthClient:
    c = NexHealthClient(
        api_key="KEY", subdomain="sunshine", location_id="42",
        base_url="https://nh.test", transport=httpx.MockTransport(handler),
    )
    c._retry_base_delay = 0  # no real sleep between retries
    return c


def _patient(i: int) -> dict:
    return {"id": i, "first_name": f"P{i}", "last_name": "X",
            "bio": {"phone_number": f"+1555000{i:04d}"}, "preferred_language": "EN"}


# ── mock source ────────────────────────────────────────────────────────────
async def test_mock_source_covers_segments_and_survives_dirty():
    recs = await MockReactivationSource().pull_reactivation_records()
    assert len(recs) == 5
    ids = {r.pms_external_id for r in recs}
    assert "nh-1005" in ids  # dirty/partial record still returned (no crash)
    assert any(r.preferred_language == "es" for r in recs)
    assert any(r.contactable is False for r in recs)  # opted-out present


async def test_mock_source_incremental_filter():
    cutoff = date(2026, 6, 1)
    recs = await MockReactivationSource().pull_reactivation_records(updated_since=cutoff)
    # Only records with last_visit_date >= cutoff (the active-ish one).
    assert all(r.last_visit_date and r.last_visit_date >= cutoff for r in recs)


# ── real client (mocked HTTP) ─────────────────────────────────────────────────
async def test_auth_then_paginated_pull():
    seen = {"auth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # Cloudflare requires a real UA on EVERY call (else 1010/403) — assert it.
        assert (request.headers.get("user-agent") or "").startswith("Dentovox")
        if request.url.path == "/authenticates":
            seen["auth"] += 1
            assert request.headers.get("authorization") == "KEY"
            return httpx.Response(200, json={"data": {"token": "TOK"}})
        # /patients
        assert request.headers.get("authorization") == "Bearer TOK"
        assert request.url.params["subdomain"] == "sunshine"
        assert request.url.params["location_id"] == "42"
        page = int(request.url.params["page"])
        rows = [_patient(i) for i in range(100)] if page == 1 else [_patient(i) for i in range(3)]
        return httpx.Response(200, json={"data": {"patients": rows}})

    recs = await _client(handler).pull_reactivation_records(limit=1000)
    assert len(recs) == 103  # page1 (100) + page2 (3), stops on short page
    assert recs[0].phone == "+15550000000"
    assert recs[0].preferred_language == "en"  # normalized lowercase
    assert seen["auth"] == 1  # token fetched once, reused across pages


async def test_dirty_patient_without_id_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        return httpx.Response(200, json={"data": {"patients": [
            {"first_name": "NoId"},          # no id → skipped
            {"id": 7, "first_name": "Ok"},   # kept
        ]}})

    recs = await _client(handler).pull_reactivation_records()
    assert [r.pms_external_id for r in recs] == ["7"]


async def test_balance_and_contactability_mapping():
    """Real NexHealth patient shape (confirmed against sandbox): balance in
    dollars on the patient; not-contactable if unsubscribe_sms OR inactive."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        return httpx.Response(200, json={"data": {"patients": [
            {"id": 1, "balance": "45.50", "unsubscribe_sms": False, "inactive": False},
            {"id": 2, "balance": "0", "unsubscribe_sms": True},   # opted out
            {"id": 3, "inactive": True},                          # inactive
        ]}})

    recs = {r.pms_external_id: r for r in await _client(handler).pull_reactivation_records()}
    assert recs["1"].balance_cents == 4550 and recs["1"].contactable is True
    assert recs["2"].contactable is False  # unsubscribe_sms
    assert recs["3"].contactable is False  # inactive
    assert recs["3"].balance_cents == 0    # missing balance → 0, no crash


async def test_balance_object_shape_confirmed_sandbox():
    """CONFIRMED sandbox 2026-06-26: balance is an object {amount, currency}, not a
    flat string. _balance_to_cents must read .amount."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        if request.url.path == "/patients":
            return httpx.Response(200, json={"data": {"patients": [
                {"id": 1, "balance": {"amount": "12.50", "currency": "USD"}},
                {"id": 2, "balance": {"amount": "0", "currency": "USD"}},
            ]}})
        return httpx.Response(200, json={"data": []})  # /appointments → no visits

    recs = {r.pms_external_id: r for r in await _client(handler).pull_reactivation_records()}
    assert recs["1"].balance_cents == 1250
    assert recs["2"].balance_cents == 0


async def test_last_visit_enriched_from_appointments():
    """last_visit is NOT on the patient; it's the newest non-cancelled past
    appointment from /appointments (CONFIRMED sandbox)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        if request.url.path == "/patients":
            return httpx.Response(200, json={"data": {"patients": [
                {"id": 1}, {"id": 2}, {"id": 3},
            ]}})
        # /appointments — patient 1 has two past visits, patient 2 only a cancelled
        # one, patient 3 none.
        return httpx.Response(200, json={"data": [
            {"patient_id": 1, "start_time": "2024-01-10T09:00:00.000Z"},
            {"patient_id": 1, "start_time": "2025-03-02T09:00:00.000Z"},  # newest
            {"patient_id": 2, "start_time": "2025-05-01T09:00:00.000Z", "cancelled": True},
        ]})

    recs = {r.pms_external_id: r for r in await _client(handler).pull_reactivation_records()}
    assert recs["1"].last_visit_date == date(2025, 3, 2)  # most recent, not cancelled
    assert recs["2"].last_visit_date is None              # only a cancelled appt
    assert recs["3"].last_visit_date is None              # no appts


async def test_find_slots_sends_lids_and_slot_length():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        captured["lids"] = request.url.params.get("lids[]")
        captured["slot_length"] = request.url.params.get("slot_length")
        captured["pids"] = request.url.params.get("pids[]")
        return httpx.Response(200, json={"data": [
            {"lid": 42, "pid": 7, "slots": [{"time": "2026-07-06T13:00:00-05:00",
                                             "end_time": "x", "operatory_id": 3}]},
        ]})

    slots = await _client(handler).find_appointment_slots(
        start_date="2026-07-06", days=5, provider_ids=["7"], slot_length=30)
    assert captured["lids"] == "42"          # location_id scoped in as lids[]
    assert captured["slot_length"] == "30"
    assert captured["pids"] == "7"
    assert slots[0].provider_id == "7" and slots[0].operatory_id == "3"


async def test_create_appointment_parses_flat_data():
    """CONFIRMED sandbox 201: response data is the appt object directly."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        assert request.method == "POST"
        body = httpx.Request("POST", request.url, content=request.content).content
        assert b"appointment_type_id" not in body  # never sent (sandbox rejects it)
        return httpx.Response(201, json={"data": {
            "id": 999, "start_time": "2026-07-06T13:00:00.000Z", "end_time": "x"}})

    appt = await _client(handler).create_appointment(
        patient_pms_id="1", provider_id="7", start_time="2026-07-06T13:00:00-05:00",
        operatory_id="3", note="Dentovox reactivation")
    assert appt.appointment_id == "999"
    assert appt.start_time == "2026-07-06T13:00:00.000Z"


async def test_401_triggers_token_refresh():
    state = {"patients_calls": 0, "auth_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            state["auth_calls"] += 1
            return httpx.Response(200, json={"data": {"token": f"T{state['auth_calls']}"}})
        state["patients_calls"] += 1
        if state["patients_calls"] == 1:
            return httpx.Response(401)  # expired token → client must re-auth + retry
        return httpx.Response(200, json={"data": {"patients": [{"id": 1}]}})

    recs = await _client(handler).pull_reactivation_records()
    assert len(recs) == 1
    assert state["auth_calls"] == 2  # re-authenticated after the 401


async def test_find_slots_fills_in_the_providers_when_the_caller_names_none():
    """pids[] is REQUIRED by NexHealth, and we were not sending it.

    Every availability question during a live call went out without it, came
    back 400, and the caller silently fell back to our own calendar. A clinic
    whose PMS was connected and healthy was still offered times from our book,
    and no booking ever reached its real calendar — pms_external_id was NULL on
    every appointment the product has ever taken.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        seen.append(request.url.path)
        if request.url.path == "/providers":
            return httpx.Response(200, json={"data": [
                {"id": 11}, {"id": 12, "inactive": True}, {"id": 13},
            ]})
        assert request.url.params.get_list("pids[]") == ["11", "13"], (
            "the slot query went out without providers"
        )
        return httpx.Response(200, json={"data": [
            {"lid": 42, "pid": 11, "slots": [{"time": "2026-07-06T13:00:00-05:00",
                                              "end_time": "x", "operatory_id": 3}]},
        ]})

    client = _client(handler)
    slots = await client.find_appointment_slots(start_date="2026-07-06", days=5)
    assert slots and slots[0].provider_id == "11"
    assert "/providers" in seen

    # The roster is asked for once, not on every question in a call.
    await client.find_appointment_slots(start_date="2026-07-07", days=5)
    assert seen.count("/providers") == 1


async def test_no_providers_means_no_slot_query_at_all():
    """Nobody bookable is an answer, not a 400. Asking anyway wastes a request
    mid-call and returns an error the caller would read as a PMS outage."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        if request.url.path == "/providers":
            return httpx.Response(200, json={"data": []})
        raise AssertionError("asked for slots with no providers")

    assert await _client(handler).find_appointment_slots(
        start_date="2026-07-06", days=5) == []


async def test_first_provider_id_shares_the_same_roster():
    """It used to make its own /providers request with a different page size.
    Two lookups of one fact drift; this one is used to attach a new patient."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": 99}]})

    client = _client(handler)
    assert await client.first_provider_id() == "99"
    assert await client.first_provider_id() == "99"
    assert calls.count("/providers") == 1
