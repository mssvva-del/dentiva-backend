"""Open Dental Step 2 — real OpenDentalClient against MOCKED HTTP.

No network: every Open Dental response is stubbed with httpx.MockTransport, so we
verify the client builds the right requests (auth header, paths, params, body)
and parses OD's field names (PatNum/AptNum/ProvNum/OpNum/AptDateTime) correctly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.open_dental.client import (
    OpenDentalClient,
    PMSError,
    PMSUnavailable,
    _pattern_for,
    _to_od_datetime,
)


def _client(handler) -> OpenDentalClient:
    """A client whose HTTP layer is the given MockTransport handler."""
    return OpenDentalClient(
        developer_key="DEVKEY", customer_key="CUSTKEY",
        base_url="https://od.test/api/v1",
        transport=httpx.MockTransport(handler),
    )


# ── helpers ──────────────────────────────────────────────────────────────────
def test_to_od_datetime_and_pattern():
    assert _to_od_datetime("2026-06-10T09:00") == "2026-06-10 09:00:00"
    assert _to_od_datetime("2026-06-10T09:30:00") == "2026-06-10 09:30:00"
    assert _pattern_for(60) == "/" * 12
    assert _pattern_for(30) == "/" * 6


# ── auth header ──────────────────────────────────────────────────────────────
async def test_auth_header_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"PatNum": 1, "FName": "A", "LName": "B"})

    await _client(handler).get_patient("1")
    assert seen["auth"] == "ODFHIR DEVKEY/CUSTKEY"  # confirmed scheme


# ── slots ────────────────────────────────────────────────────────────────────
async def test_check_availability_parses_slots():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/appointments/Slots")
        assert request.url.params["ProvNum"] == "2"
        assert request.url.params["lengthMinutes"] == "60"
        return httpx.Response(200, json=[
            {"DateTimeStart": "2026-06-10 09:00:00", "ProvNum": 2, "OpNum": 5},
            {"DateTimeStart": "2026-06-10 10:30:00", "ProvNum": 2, "OpNum": 5},
        ])

    slots = await _client(handler).check_availability(
        "2026-06-10", "2026-06-10", prov_num="2", length_minutes=60
    )
    assert len(slots) == 2
    assert slots[0].date == "2026-06-10" and slots[0].time == "09:00"
    assert slots[0].prov_num == "2" and slots[0].op_num == "5"


# ── create / get / reschedule / cancel ───────────────────────────────────────
async def test_create_appointment_builds_body_and_parses():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/appointments")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "AptNum": 555, "PatNum": 1, "AptDateTime": "2026-06-10 09:00:00",
            "Pattern": "/" * 12, "ProvNum": 2, "ProcDescript": "Cleaning",
        })

    appt = await _client(handler).create_appointment(
        "1", "2026-06-10T09:00", "cleaning", 60, op_num="5", prov_num="2"
    )
    assert captured["body"]["PatNum"] == "1"
    assert captured["body"]["AptDateTime"] == "2026-06-10 09:00:00"
    assert captured["body"]["Op"] == "5" and captured["body"]["ProvNum"] == "2"
    assert appt.apt_num == "555" and appt.duration_minutes == 60


async def test_reschedule_puts_to_apt_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "AptNum": 555, "PatNum": 1, "AptDateTime": "2026-06-11 10:00:00",
            "Pattern": "/" * 12, "ProvNum": 2,
        })

    appt = await _client(handler).reschedule_appointment("555", "2026-06-11T10:00")
    assert seen["method"] == "PUT" and seen["path"].endswith("/appointments/555")
    assert seen["body"]["AptDateTime"] == "2026-06-11 10:00:00"
    assert appt.appointment_at == "2026-06-11 10:00:00"


async def test_cancel_calls_break_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    ok = await _client(handler).cancel_appointment("555")
    assert ok is True
    assert seen["path"].endswith("/appointments/555/Break")
    assert seen["body"]["sendToUnscheduledList"] == "true"


async def test_get_patient_by_phone_returns_first_match():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["phone"] == "+15551234567"
        return httpx.Response(200, json=[
            {"PatNum": 7, "FName": "Maria", "LName": "Garcia",
             "WirelessPhone": "+15551234567", "Email": "m@x.com"},
        ])

    p = await _client(handler).get_patient_by_phone("+15551234567")
    assert p and p.pms_external_id == "7" and p.first_name == "Maria"


async def test_providers_and_operatories_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/providers"):
            return httpx.Response(200, json=[
                {"ProvNum": 1, "FName": "John", "LName": "Smith", "Abbr": "SMI"},
            ])
        return httpx.Response(200, json=[
            {"OperatoryNum": 3, "OpName": "Op 3", "ProvDentist": 1},
        ])

    c = _client(handler)
    provs = await c.list_providers()
    ops = await c.list_operatories()
    assert provs[0].prov_num == "1" and provs[0].name == "John Smith"
    assert ops[0].op_num == "3" and ops[0].prov_num == "1"


# ── error mapping ────────────────────────────────────────────────────────────
async def test_4xx_raises_pms_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad slot")

    with pytest.raises(PMSError):
        await _client(handler).create_appointment("1", "2026-06-10T09:00", "x", 60)


async def test_5xx_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(PMSUnavailable):
        await _client(handler).list_providers()


async def test_get_appointment_404_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    assert await _client(handler).get_appointment("999") is None


# ── self-review fixes (PHI-in-logs + cross-tenant key) ───────────────────────
async def test_4xx_error_does_not_leak_response_body():
    # An OD error body could echo PHI (a patient name). It must NOT appear in the
    # raised exception (which could land in logs / an API response).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Patient Maria Garcia +15551234567 invalid")

    with pytest.raises(PMSError) as ei:
        await _client(handler).get_patient_by_phone("+15551234567")
    msg = str(ei.value)
    assert "Maria" not in msg and "15551234567" not in msg  # body scrubbed
    assert "400" in msg  # status still useful


async def test_request_refuses_without_customer_key():
    # A client with no customer key must NOT call Open Dental (cross-tenant guard).
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the network without a key")

    client = OpenDentalClient(
        developer_key="DEV", customer_key="", base_url="https://od.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PMSError):
        await client.list_providers()


def test_factory_per_practice_open_dental_requires_customer_key():
    from app.adapters.open_dental import get_pms_adapter

    # Per-practice real OD without an explicit key → refuse (no silent env fallback).
    with pytest.raises(ValueError):
        get_pms_adapter(pms_system="open_dental", customer_key=None)
    # mock path is always fine.
    assert get_pms_adapter(pms_system="mock") is not None


# ── resilience: retry idempotent reads, never retry writes (Addition 3) ───────
async def test_get_retried_on_transient_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)  # transient -> PMSUnavailable -> retry
        return httpx.Response(200, json={"PatNum": 1, "FName": "A", "LName": "B"})

    c = _client(handler)
    c._retry_base_delay = 0  # no real sleep in tests
    patient = await c.get_patient("1")
    assert patient is not None
    assert calls["n"] == 2  # retried once, second try won


async def test_get_4xx_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)  # 4xx -> PMSError, NOT a transient

    c = _client(handler)
    c._retry_base_delay = 0
    assert await c.get_patient("1") is None
    assert calls["n"] == 1  # PMSError is not in retry_on


async def test_post_write_not_retried_on_transient():
    """A retried POST could double-book — writes must be single-shot."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    c = _client(handler)
    c._retry_base_delay = 0
    with pytest.raises(PMSUnavailable):
        await c.create_patient("A", "B", "+15551234567")
    assert calls["n"] == 1  # no retry on a write
