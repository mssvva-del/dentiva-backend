"""NexHealth reactivation pull — mock source + real client against mocked HTTP.

No network: NexHealth responses are stubbed with httpx.MockTransport so we verify
auth flow, pagination, dirty-data tolerance, and token refresh without live access.
"""

from __future__ import annotations

from datetime import date

import httpx

from app.adapters.nexhealth import get_reactivation_source
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


async def test_factory_returns_mock_without_keys(settings):
    # settings fixture clears cache; no NexHealth keys set in test env.
    assert isinstance(get_reactivation_source(), MockReactivationSource)


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
