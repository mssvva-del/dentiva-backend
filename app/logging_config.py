"""Structured JSON logging + request context (observability; Sentry-prep).

Why: prod logs must be machine-parseable and correlatable. Every line carries a
``request_id`` (so all logs for one webhook/API call group together) and the
``practice_id`` (so we can trace one clinic) — WITHOUT ever emitting PHI.

PHI scrub: a defense-in-depth filter redacts phone numbers and emails from log
text. It is NOT a license to log PHI — code must still avoid logging names and
transcripts — but it catches accidental leaks (e.g. an exception repr that
happens to carry a phone). When we wire Sentry, these same contextvars + scrub
feed its ``before_send``, so PHI never leaves the box.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar

# Per-request context. Set by RequestContextMiddleware (request_id) and by
# set_tenant (practice_id); read by the formatter. Default None → omitted.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
practice_id_var: ContextVar[str | None] = ContextVar("practice_id", default=None)

# Contiguous 10–15 digit run (optionally +-prefixed) = a phone in this codebase
# (E.164 like +15551234567). Tight on purpose: it must NOT eat dates/timestamps
# ("2026-06-16" maxes at 4-digit runs) or short ids.
_PHONE_RE = re.compile(r"\+?\d{10,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub_phi(text: str) -> str:
    """Best-effort redaction of phones/emails from a log string."""
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return _PHONE_RE.sub("[redacted-phone]", text)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with request/practice context and PHI scrub."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub_phi(record.getMessage()),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        pid = practice_id_var.get()
        if pid:
            payload["practice_id"] = pid
        if record.exc_info:
            payload["exc"] = scrub_phi(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str, *, json_logs: bool) -> None:
    """Install the root handler. JSON in prod, human-readable in dev."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
