"""Tests for the verify_security_config() startup guard.

Locks in the corrected behaviour (Block 2 fix): the guard must read the
lowercase pydantic-settings attributes. The original used
getattr(settings, "ENVIRONMENT", ...) which silently returned the default,
so is_production was ALWAYS False and none of the hard checks ever fired.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.security import verify_security_config


def _prod_settings(**overrides) -> Settings:
    base = dict(
        environment="production",
        auth_dev_bypass=False,
        retell_webhook_secret="whsec_test",
        clerk_secret_key="sk_test",
        encryption_key="enc_test",
        twilio_validate_signature=True,  # required in prod (inbound SMS webhook auth)
    )
    base.update(overrides)
    return Settings(**base)


def test_passes_in_production_when_required_secrets_present():
    # All required secrets present → no raise.
    verify_security_config(_prod_settings())


def test_raises_when_a_production_secret_is_missing():
    with pytest.raises(RuntimeError, match="RETELL_WEBHOOK_SECRET"):
        verify_security_config(_prod_settings(retell_webhook_secret=""))


def test_raises_when_demo_open_access_enabled_in_production():
    with pytest.raises(RuntimeError, match="DEMO_OPEN_ACCESS"):
        verify_security_config(_prod_settings(demo_open_access=True))


def test_raises_when_twilio_signature_off_in_production():
    # The inbound Twilio SMS webhook must be signature-verified in prod.
    with pytest.raises(RuntimeError, match="TWILIO_VALIDATE_SIGNATURE"):
        verify_security_config(_prod_settings(twilio_validate_signature=False))


def test_development_never_raises_even_with_empty_secrets():
    # Dev/staging: missing secrets are warnings, not hard failures.
    s = Settings(
        environment="development",
        retell_webhook_secret="",
        clerk_secret_key="",
        encryption_key="",
        demo_open_access=True,
    )
    verify_security_config(s)  # must not raise
