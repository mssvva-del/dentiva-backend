"""Sentry before_send PHI scrubbing (HIPAA backstop)."""

from __future__ import annotations

from app.observability.sentry import _scrub_event, init_sentry


def test_scrub_redacts_phone_and_email_everywhere():
    event = {
        "message": "booking for maria@clinic.com phone +15551234567",
        "exception": {
            "values": [
                {"type": "ValueError", "value": "patient +15559990000 not found"}
            ]
        },
        "breadcrumbs": [{"message": "called +15557778888"}],
        "extra": {"note": "ring back +15551112222"},
    }
    out = _scrub_event(event, {})

    assert "+15551234567" not in out["message"]
    assert "maria@clinic.com" not in out["message"]
    assert "[redacted-phone]" in out["message"]
    assert "+15559990000" not in out["exception"]["values"][0]["value"]
    assert "+15557778888" not in out["breadcrumbs"][0]["message"]
    assert "+15551112222" not in out["extra"]["note"]


def test_scrub_drops_identity_and_request_pii():
    event = {
        "message": "err",
        "user": {"id": "u1", "email": "x@y.com", "ip_address": "1.2.3.4"},
        "request": {
            "url": "https://api/x",
            "cookies": {"session": "secret"},
            "headers": {"Authorization": "Bearer tok"},
            "data": {"patient_phone": "+15551234567"},
            "query_string": "phone=+15551234567",
        },
    }
    out = _scrub_event(event, {})

    assert "user" not in out  # identity block removed entirely
    assert "cookies" not in out["request"]
    assert "headers" not in out["request"]
    assert "data" not in out["request"]
    assert "query_string" not in out["request"]
    assert out["request"]["url"] == "https://api/x"  # non-PII kept


def test_init_sentry_noop_without_dsn():
    class _S:
        sentry_dsn = ""
        environment = "test"
        sentry_traces_sample_rate = 0.0

    # No DSN → disabled, returns False, never imports/sends.
    assert init_sentry(_S()) is False
