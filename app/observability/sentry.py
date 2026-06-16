"""Sentry error monitoring with HIPAA-grade PHI scrubbing.

We send error events to Sentry for alerting, but a dental backend handles PHI
(patient names, phone numbers, transcripts). So:

  * ``send_default_pii=False`` — Sentry never auto-attaches IPs, cookies, or
    request bodies.
  * ``before_send`` (``_scrub_event``) is a hard backstop: it drops the user/
    cookies/headers blocks outright and recursively runs ``scrub_phi`` over EVERY
    remaining string in the event (message, exception values, breadcrumbs, extra),
    so a phone/email that slipped into an exception repr is redacted before it
    leaves the process.

Disabled when ``SENTRY_DSN`` is empty (dev/tests) — ``init_sentry`` is a no-op,
so nothing is imported or sent.
"""

from __future__ import annotations

import logging
from typing import Any

from app.logging_config import scrub_phi

logger = logging.getLogger("dentiva.observability")

# Event blocks that can carry PII/PHI and have no diagnostic value worth the risk.
_DROP_REQUEST_KEYS = ("cookies", "headers", "data", "query_string")


def _scrub(obj: Any) -> Any:
    """Recursively redact phones/emails from every string in a nested structure."""
    if isinstance(obj, str):
        return scrub_phi(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub(v) for v in obj)
    return obj


def _scrub_event(event: dict, _hint: dict) -> dict:
    """Sentry before_send hook — strip PII blocks, then scrub all strings."""
    # Identity blocks: remove entirely (we never want a patient/user identified).
    event.pop("user", None)
    request = event.get("request")
    if isinstance(request, dict):
        for key in _DROP_REQUEST_KEYS:
            request.pop(key, None)
    # Backstop: redact any phone/email left anywhere in the event.
    return _scrub(event)


def init_sentry(settings) -> bool:  # noqa: ANN001
    """Initialise Sentry if a DSN is configured. Returns True when enabled."""
    if not settings.sentry_dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        send_default_pii=False,  # never auto-collect IP/cookies/bodies
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=_scrub_event,
    )
    logger.info("Sentry initialised (environment=%s, pii=off)", settings.environment)
    return True
