"""Tests for the verify_security_config() startup guard.

Locks in the corrected behaviour (Block 2 fix): the guard must read the
lowercase pydantic-settings attributes. The original used
getattr(settings, "ENVIRONMENT", ...) which silently returned the default,
so is_production was ALWAYS False and none of the hard checks ever fired.
"""

from __future__ import annotations

import pytest

import app.db as app_db
from app.config import Settings
from app.security import verify_db_security, verify_security_config


def _prod_settings(**overrides) -> Settings:
    base = dict(
        environment="production",
        auth_dev_bypass=False,
        retell_webhook_secret="whsec_test",
        clerk_secret_key="sk_test",
        encryption_key="enc_test",
        twilio_validate_signature=True,  # required in prod (inbound SMS webhook auth)
        twilio_auth_token="tw_test",     # required so signatures can actually verify
        rate_limit_enabled=True,         # required prod gate (abuse backstop)
        cors_allowed_origins="https://app.dentovox.com",  # locked, non-localhost
        # Explicit empty Stripe so the base = "billing not live" regardless of any
        # STRIPE_* leaking in from .env; the Stripe tests set these deliberately.
        stripe_secret_key="",
        stripe_webhook_secret="",
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


def test_raises_when_twilio_token_missing_with_validation_on():
    # Flag on but token empty → signatures reject every SMS. Fail loudly at boot.
    with pytest.raises(RuntimeError, match="TWILIO_AUTH_TOKEN"):
        verify_security_config(_prod_settings(twilio_auth_token=""))


def test_raises_when_rate_limit_disabled_in_production():
    with pytest.raises(RuntimeError, match="RATE_LIMIT_ENABLED"):
        verify_security_config(_prod_settings(rate_limit_enabled=False))


def test_raises_when_cors_is_localhost_in_production():
    with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
        verify_security_config(
            _prod_settings(cors_allowed_origins="http://localhost:3000")
        )


def test_raises_when_cors_empty_in_production():
    with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
        verify_security_config(_prod_settings(cors_allowed_origins=""))


def test_cors_localhost_subdomain_is_not_flagged():
    # Host is 'localhost.example.com' (a real domain), NOT the loopback host —
    # substring matching would wrongly reject it; host parsing must allow it.
    verify_security_config(
        _prod_settings(cors_allowed_origins="https://localhost.example.com")
    )


def test_cors_mixed_list_with_one_loopback_raises():
    with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
        verify_security_config(
            _prod_settings(
                cors_allowed_origins="https://app.dentovox.com,http://127.0.0.1:8000"
            )
        )


def test_raises_when_stripe_live_without_webhook_secret():
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        verify_security_config(
            _prod_settings(stripe_secret_key="sk_live_x", stripe_webhook_secret="")
        )


def test_passes_when_stripe_live_with_webhook_secret():
    verify_security_config(
        _prod_settings(stripe_secret_key="sk_live_x", stripe_webhook_secret="whsec_x")
    )


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


# ── DB-role check (RLS-bypass guard) ─────────────────────────────────────────
async def test_db_security_raises_for_superuser_role(db_session):
    # db_session connects as the OWNER 'dentiva' (SUPERUSER) → exempt from RLS →
    # must hard-fail in production.
    s = Settings(environment="production", auth_dev_bypass=False)
    with pytest.raises(RuntimeError, match="SUPERUSER|BYPASSRLS"):
        await verify_db_security(db_session, s)


async def test_db_security_passes_for_app_role(db_session):
    # The app connects as dentiva_app (NOSUPERUSER NOBYPASSRLS) → RLS enforced →
    # passes in production. (db_session param triggers DB prep + engine repoint.)
    s = Settings(environment="production", auth_dev_bypass=False)
    async with app_db.async_session_factory() as session:
        await verify_db_security(session, s)  # must not raise


async def test_db_security_noop_in_development(db_session):
    # Even connected as the superuser owner, dev/test never fails this check.
    s = Settings(environment="development", auth_dev_bypass=False)
    await verify_db_security(db_session, s)  # must not raise
