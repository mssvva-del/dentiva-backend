"""NUM-1 — a Dentovox number per clinic: area code, provisioning, routing."""

from __future__ import annotations

import uuid

import pytest

from app.services.retell_admin import RetellError, RetellNotConfigured
from app.services.telephony import provision as prov


class _P:
    """Minimal practice stand-in — provisioning only reads these fields."""

    def __init__(self, phone=None, transfer=None, name="Test Clinic", status="trial"):
        self.id = uuid.uuid4()
        self.name = name
        self.phone_number = phone
        self.transfer_phone_number = transfer
        self.status = status


def test_area_code_parsing():
    assert prov.area_code_of("+1 (718) 786-4175") == 718
    assert prov.area_code_of("7187864175") == 718
    assert prov.area_code_of("17187864175") == 718
    assert prov.area_code_of("+13105551234") == 310
    # Unusable inputs → None (we let the provider pick rather than guess wrong).
    assert prov.area_code_of(None) is None
    assert prov.area_code_of("") is None
    assert prov.area_code_of("12345") is None
    assert prov.area_code_of("+44 20 7946 0958") is None  # not NANP
    # Falls through to the next candidate.
    assert prov.area_code_of(None, "(212) 555-0100") == 212


async def test_provision_uses_clinic_area_code_and_binds_agent(monkeypatch):
    calls: list[dict] = []

    async def _fake_request(method, path, payload=None, *, transport=None):
        calls.append({"method": method, "path": path, "payload": payload})
        return {"phone_number": "+17185550143"}

    monkeypatch.setattr(prov, "_request", _fake_request)
    monkeypatch.setattr(prov, "get_settings", lambda: type("S", (), {
        "retell_agent_id": "agent_x",
        "public_base_url": "https://api.example.com",
    })())

    number = await prov.provision_number_for_practice(_P(phone="+17187864175"))
    assert number == "+17185550143"
    sent = calls[0]["payload"]
    assert calls[0]["path"] == "/create-phone-number"
    assert sent["area_code"] == 718                      # clinic's own area code
    assert sent["inbound_agents"] == [{"agent_id": "agent_x"}]  # bound on creation
    # The inbound webhook must be OUR public origin, else the number answers with
    # no clinic context.
    assert sent["inbound_webhook_url"] == "https://api.example.com/webhooks/retell/inbound"


async def test_provision_retries_without_area_code_when_sold_out(monkeypatch):
    attempts: list[dict | None] = []

    async def _fake_request(method, path, payload=None, *, transport=None):
        # Copy: the retry reuses (and mutates) the same dict.
        attempts.append(dict(payload or {}))
        if len(attempts) == 1:
            raise RetellError("no numbers in 718", status_code=400)
        return {"phone_number": "+19995550100"}

    monkeypatch.setattr(prov, "_request", _fake_request)
    monkeypatch.setattr(prov, "get_settings", lambda: type("S", (), {
        "retell_agent_id": "agent_x", "public_base_url": "https://api.example.com",
    })())

    number = await prov.provision_number_for_practice(_P(phone="+17187864175"))
    assert number == "+19995550100"
    assert "area_code" in attempts[0] and "area_code" not in attempts[1]


async def test_provision_requires_an_agent_to_bind(monkeypatch):
    monkeypatch.setattr(prov, "get_settings", lambda: type("S", (), {
        "retell_agent_id": "", "public_base_url": "https://api.example.com",
    })())
    with pytest.raises(RetellNotConfigured):
        await prov.provision_number_for_practice(_P(phone="+17187864175"))


async def test_provision_surfaces_server_errors(monkeypatch):
    async def _fake_request(method, path, payload=None, *, transport=None):
        raise RetellError("provider down", status_code=502)

    monkeypatch.setattr(prov, "_request", _fake_request)
    monkeypatch.setattr(prov, "get_settings", lambda: type("S", (), {
        "retell_agent_id": "agent_x", "public_base_url": "https://api.example.com",
    })())
    with pytest.raises(RetellError):
        await prov.provision_number_for_practice(_P(phone="+17187864175"))


async def test_number_requires_a_commercial_commitment(monkeypatch):
    """A number bills monthly forever, and most signups never buy. A practice
    still evaluating gets no number — the browser demo covers evaluation."""
    called = False

    async def _fake_request(*a, **kw):
        nonlocal called
        called = True
        return {"phone_number": "+15550000000"}

    monkeypatch.setattr(prov, "_request", _fake_request)
    monkeypatch.setattr(prov, "get_settings", lambda: type("S", (), {
        "retell_agent_id": "agent_x", "public_base_url": "https://api.example.com",
    })())

    with pytest.raises(prov.NotEntitledToNumber):
        await prov.provision_number_for_practice(_P(phone="+17187864175", status="onboarding"))
    assert called is False, "must not reach the provider — that's where the money goes"

    # And the states that DO get one.
    for status in ("trial", "pilot", "active"):
        assert await prov.provision_number_for_practice(
            _P(phone="+17187864175", status=status)
        ) == "+15550000000"

    # Churned practices don't get new numbers either.
    for status in ("suspended", "cancelled"):
        with pytest.raises(prov.NotEntitledToNumber):
            await prov.provision_number_for_practice(_P(phone="+17187864175", status=status))
