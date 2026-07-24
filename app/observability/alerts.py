"""Operational alerts — surface HANDLED failures we'd otherwise only see in logs.

Unhandled exceptions already reach Sentry. But the failures that hurt a live
clinic are usually CAUGHT and logged as warnings — a rejected confirmation SMS, a
Retell tool erroring, a web-call the provider refused. Those must page us BEFORE a
clinic notices.

``record_alert`` does two things, both no-op-safe without extra config:
  * logs at ERROR (Sentry's logging integration turns ERROR logs into events →
    email/Slack alert when SENTRY_DSN is set), with PHI-safe context only, and
  * bumps a tiny in-process ring buffer that ``/health/detailed`` exposes, so an
    external uptime check (UptimeRobot) can see "N failures in the last hour"
    even with no Sentry.

Never pass PHI (names, phone numbers, transcripts) in ``detail``/context — pass
codes, counts, and ids only.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from threading import Lock

logger = logging.getLogger("dentiva.alerts")

# (epoch_seconds, kind, detail) — bounded; only the last hour is ever reported.
_RECENT: deque[tuple[float, str, str]] = deque(maxlen=200)
_LOCK = Lock()
_WINDOW_S = 3600


def record_alert(kind: str, detail: str = "", *, now: float | None = None) -> None:
    """Record a critical operational failure. kind = short slug
    (e.g. 'twilio_send_failed', 'retell_tool_error', 'web_call_failed')."""
    ts = now if now is not None else time.time()
    with _LOCK:
        _RECENT.append((ts, kind, detail[:200]))
    logger.error("ALERT %s: %s", kind, detail[:200])  # Sentry captures ERROR logs


def recent_alerts(*, now: float | None = None) -> dict:
    """Summary of alerts in the last hour for the health endpoint (no PHI)."""
    cutoff = (now if now is not None else time.time()) - _WINDOW_S
    with _LOCK:
        recent = [(ts, k, d) for ts, k, d in _RECENT if ts >= cutoff]
    by_kind: dict[str, int] = {}
    for _ts, k, _d in recent:
        by_kind[k] = by_kind.get(k, 0) + 1
    last = recent[-1] if recent else None
    return {
        "count_last_hour": len(recent),
        "by_kind": by_kind,
        "last_kind": last[1] if last else None,
        # detail is codes/counts/ids only (never PHI, by contract above) — safe to
        # expose so remote diagnosis sees e.g. "retell_status=404" without logs.
        "last_detail": last[2] if last else None,
    }
