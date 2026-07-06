"""Retell webhook signature verification — matches Retell's v=,d= HMAC scheme."""

from __future__ import annotations

import hashlib
import hmac

import app.webhooks.retell as retell_mod


def _sign(secret: str, body: bytes, ts_ms: int) -> str:
    """Reproduce Retell SDK's signer: HMAC-SHA256(secret, body + str(ts)) hex,
    wrapped as the header 'v={ts},d={hex}'."""
    digest = hmac.new(secret.encode(), body + str(ts_ms).encode(), hashlib.sha256).hexdigest()
    return f"v={ts_ms},d={digest}"


class _Cfg:
    retell_webhook_secret = "key_secret_x"
    environment = "production"


def test_valid_signature_accepted(monkeypatch):
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Cfg())
    body = b'{"event":"call_analyzed","call":{"call_id":"c1"}}'
    ts = 1_800_000_000_000
    sig = _sign("key_secret_x", body, ts)
    assert retell_mod._verify_signature(body, sig, now_ms=ts + 1000) is True


def test_tampered_body_rejected(monkeypatch):
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Cfg())
    ts = 1_800_000_000_000
    sig = _sign("key_secret_x", b'{"amount":1}', ts)
    assert retell_mod._verify_signature(b'{"amount":999}', sig, now_ms=ts) is False


def test_wrong_secret_rejected(monkeypatch):
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Cfg())
    body = b"{}"
    ts = 1_800_000_000_000
    sig = _sign("some_other_key", body, ts)  # signed with the wrong key
    assert retell_mod._verify_signature(body, sig, now_ms=ts) is False


def test_stale_timestamp_rejected(monkeypatch):
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Cfg())
    body = b"{}"
    ts = 1_800_000_000_000
    sig = _sign("key_secret_x", body, ts)
    # 6 minutes later → outside the 5-minute tolerance.
    assert retell_mod._verify_signature(body, sig, now_ms=ts + 6 * 60 * 1000) is False


def test_malformed_or_legacy_header_rejected(monkeypatch):
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Cfg())
    body = b"{}"
    # A bare hex (our OLD scheme) has no v=,d= → rejected, no crash.
    bare = hmac.new(b"key_secret_x", body, hashlib.sha256).hexdigest()
    assert retell_mod._verify_signature(body, bare, now_ms=1_800_000_000_000) is False
    assert retell_mod._verify_signature(body, None, now_ms=1) is False
    assert retell_mod._verify_signature(body, "garbage", now_ms=1) is False


def test_unset_secret_skips_in_dev(monkeypatch):
    class _Dev:
        retell_webhook_secret = ""
        environment = "development"
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Dev())
    assert retell_mod._verify_signature(b"{}", None) is True  # dev skip (logged)
