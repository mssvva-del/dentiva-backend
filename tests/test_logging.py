"""Structured logging + request_id middleware."""

from __future__ import annotations

import json
import logging

from app.logging_config import (
    JsonFormatter,
    practice_id_var,
    request_id_var,
    scrub_phi,
)


def test_scrub_phi_redacts_phone_and_email():
    assert scrub_phi("call +15551234567 now") == "call [redacted-phone] now"
    assert scrub_phi("to maria@clinic.com ok") == "to [redacted-email] ok"
    # A date must NOT be mistaken for a phone (no contiguous 10+ digit run).
    assert "2026-06-16" in scrub_phi("on 2026-06-16 at 9am")


def test_json_formatter_emits_context_and_scrubs():
    rid_token = request_id_var.set("req-abc")
    pid_token = practice_id_var.set("pract-1")
    try:
        rec = logging.LogRecord(
            name="dentiva.test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="patient phone +15559990000", args=(), exc_info=None,
        )
        line = JsonFormatter().format(rec)
        payload = json.loads(line)
    finally:
        request_id_var.reset(rid_token)
        practice_id_var.reset(pid_token)

    assert payload["request_id"] == "req-abc"
    assert payload["practice_id"] == "pract-1"
    assert payload["level"] == "INFO"
    assert "[redacted-phone]" in payload["msg"]
    assert "+15559990000" not in payload["msg"]


def test_json_formatter_omits_absent_context():
    # No contextvars set → keys are omitted entirely (not null).
    rec = logging.LogRecord(
        name="x", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(rec))
    assert "request_id" not in payload
    assert "practice_id" not in payload


async def test_request_id_echoed_when_provided(client):
    resp = await client.get("/health", headers={"X-Request-ID": "trace-xyz-1"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "trace-xyz-1"


async def test_request_id_generated_when_absent(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    rid = resp.headers.get("x-request-id")
    assert rid and len(rid) >= 8


async def test_request_id_sanitized(client):
    # Newline/garbage in the inbound header must be stripped (no log injection).
    resp = await client.get("/health", headers={"X-Request-ID": "bad\nid <script>"})
    rid = resp.headers["x-request-id"]
    assert "\n" not in rid and "<" not in rid and ">" not in rid
