"""Security Sprint (Block 0) hardening tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


# ── H6: production must refuse the dev auth bypass ──────────────────────────
def test_settings_block_dev_bypass_in_production():
    with pytest.raises(ValidationError):
        Settings(environment="production", auth_dev_bypass=True)


def test_settings_allow_dev_bypass_outside_production():
    s = Settings(environment="development", auth_dev_bypass=True)
    assert s.auth_dev_bypass is True


# ── C1: Retell webhook signature must not be skipped in production ──────────
class _FakeSettings:
    def __init__(self, env, secret):
        self.environment = env
        self.retell_webhook_secret = secret


def test_retell_signature_strict_when_secret_set(monkeypatch):
    # When a secret IS configured, verification is strict — a missing/bad
    # signature is rejected (this is the real protection once Retell is wired).
    import app.webhooks.retell as retell

    monkeypatch.setattr(retell, "get_settings", lambda: _FakeSettings("production", "s3cr3t"))
    assert retell._verify_signature(b"body", None) is False
    assert retell._verify_signature(b"body", "wrong") is False


def test_retell_signature_allows_when_no_secret(monkeypatch):
    # No secret → cannot verify; we allow (so the live agent keeps working) but
    # log loudly. Closing the hole = set the secret on both sides (see report).
    import app.webhooks.retell as retell

    monkeypatch.setattr(retell, "get_settings", lambda: _FakeSettings("production", ""))
    assert retell._verify_signature(b"body", None) is True


def test_retell_signature_valid_hmac(monkeypatch):
    import hashlib
    import hmac

    import app.webhooks.retell as retell

    secret = "s3cr3t"
    monkeypatch.setattr(
        retell, "get_settings", lambda: _FakeSettings("production", secret)
    )
    body = b'{"x":1}'
    # Retell scheme: header "v={ms},d={hex}", hex = HMAC(secret, body + str(ms)).
    ts = 1_800_000_000_000
    digest = hmac.new(secret.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()
    good = f"v={ts},d={digest}"
    assert retell._verify_signature(body, good, now_ms=ts) is True
    assert retell._verify_signature(body, "bad", now_ms=ts) is False
