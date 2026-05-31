"""Tests for the Twilio booking-confirmation SMS service."""

from __future__ import annotations

from types import SimpleNamespace

from app.services import sms


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_normalize_phone_variants():
    assert sms.normalize_phone("(555) 123-4567") == "+15551234567"
    assert sms.normalize_phone("555-123-4567") == "+15551234567"
    assert sms.normalize_phone("5551234567") == "+15551234567"
    assert sms.normalize_phone("15551234567") == "+15551234567"
    assert sms.normalize_phone("+15551234567") == "+15551234567"
    assert sms.normalize_phone("123") is None
    assert sms.normalize_phone("") is None
    assert sms.normalize_phone(None) is None


def test_build_confirmation_body_contains_key_facts():
    body = sms.build_confirmation_body(
        practice_name="Smile Dental",
        first_name="Sarah",
        date="2026-06-02",
        time="10:00",
        provider="Dr. Smith",
    )
    assert "Sarah" in body
    assert "Smile Dental" in body
    assert "2026-06-02" in body
    assert "10:00" in body
    assert "Dr. Smith" in body


def test_build_confirmation_body_handles_unknown_name():
    body = sms.build_confirmation_body(
        practice_name="Smile Dental",
        first_name="Unknown",
        date="2026-06-02",
        time="10:00",
        provider=None,
    )
    assert "Unknown" not in body  # we drop the placeholder greeting
    assert body.startswith("Hi, ")


# --------------------------------------------------------------------------- #
# Settings + fake client harness
# --------------------------------------------------------------------------- #
def _patch_settings(
    monkeypatch,
    *,
    enabled=True,
    sid="ACxxx",
    token="tok",
    sender="+15550000000",
):
    monkeypatch.setattr(
        sms,
        "get_settings",
        lambda: SimpleNamespace(
            sms_enabled=enabled,
            twilio_account_sid=sid,
            twilio_auth_token=token,
            twilio_from_number=sender,
        ),
    )


class _FakeResponse:
    def __init__(self, status_code=201, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient with a recording post()."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def post(self, url, data=None, auth=None):
        self.calls.append({"url": url, "data": data, "auth": auth})
        return self._response

    async def aclose(self):  # pragma: no cover - not used (we own the client)
        pass


# --------------------------------------------------------------------------- #
# send_sms behaviour
# --------------------------------------------------------------------------- #
async def test_send_sms_skips_when_disabled(monkeypatch):
    _patch_settings(monkeypatch, enabled=False)
    result = await sms.send_sms("+15551234567", "hi")
    assert result == {"skipped": "sms_disabled"}


async def test_send_sms_skips_when_not_configured(monkeypatch):
    _patch_settings(monkeypatch, sender="")
    result = await sms.send_sms("+15551234567", "hi")
    assert result == {"skipped": "not_configured"}


async def test_send_sms_skips_when_opted_out(monkeypatch):
    # Opt-out short-circuits BEFORE any config/number checks — even fully
    # configured + SMS-enabled, a STOPed patient gets nothing.
    _patch_settings(monkeypatch)
    result = await sms.send_sms("+15551234567", "hi", opted_out=True)
    assert result == {"skipped": "opted_out"}


async def test_send_sms_skips_bad_number(monkeypatch):
    _patch_settings(monkeypatch)
    result = await sms.send_sms("123", "hi")
    assert result == {"skipped": "bad_number"}


async def test_send_sms_success(monkeypatch):
    _patch_settings(monkeypatch)
    client = _FakeClient(_FakeResponse(201, {"sid": "SM123"}))
    result = await sms.send_sms("(555) 123-4567", "hello there", client=client)
    assert result == {"sent": True, "sid": "SM123"}
    # Number normalized to E.164 and basic auth passed through.
    assert client.calls[0]["data"]["To"] == "+15551234567"
    assert client.calls[0]["data"]["From"] == "+15550000000"
    assert client.calls[0]["auth"] == ("ACxxx", "tok")


async def test_send_sms_twilio_rejection(monkeypatch):
    _patch_settings(monkeypatch)
    client = _FakeClient(_FakeResponse(400, text="bad request"))
    result = await sms.send_sms("+15551234567", "hi", client=client)
    assert result == {"error": "twilio_rejected", "status": 400}


async def test_send_sms_network_failure_is_swallowed(monkeypatch):
    _patch_settings(monkeypatch)

    class _BoomClient:
        async def post(self, *a, **kw):
            raise RuntimeError("connection reset")

    result = await sms.send_sms("+15551234567", "hi", client=_BoomClient())
    assert result["error"] == "send_failed"
    assert "connection reset" in result["detail"]


async def test_send_booking_confirmation_sends_built_body(monkeypatch):
    _patch_settings(monkeypatch)
    client = _FakeClient(_FakeResponse(201, {"sid": "SM999"}))
    result = await sms.send_booking_confirmation(
        to="5551234567",
        practice_name="Smile Dental",
        first_name="Sarah",
        date="2026-06-02",
        time="10:00",
        provider="Dr. Smith",
        client=client,
    )
    assert result == {"sent": True, "sid": "SM999"}
    sent_body = client.calls[0]["data"]["Body"]
    assert "Sarah" in sent_body and "Smile Dental" in sent_body
