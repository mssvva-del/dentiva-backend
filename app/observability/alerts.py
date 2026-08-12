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

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from threading import Lock

logger = logging.getLogger("dentiva.alerts")

# (epoch_seconds, kind, detail) — bounded; only the last hour is ever reported.
_RECENT: deque[tuple[float, str, str]] = deque(maxlen=200)
_LOCK = Lock()
# Keeps the detached writes alive until they finish.
_WRITES: set = set()
_WINDOW_S = 3600


def record_alert(kind: str, detail: str = "", *, now: float | None = None) -> None:
    """Record a critical operational failure. kind = short slug
    (e.g. 'twilio_send_failed', 'retell_tool_error', 'web_call_failed')."""
    ts = now if now is not None else time.time()
    with _LOCK:
        _RECENT.append((ts, kind, detail[:200]))
    logger.error("ALERT %s: %s", kind, detail[:200])  # Sentry captures ERROR logs
    _persist(kind, detail[:200])


def _persist(kind: str, detail: str) -> None:
    """Write the alert down, so a redeploy cannot erase it.

    The buffer above is per-process and Railway redeploys on every merge, so the
    window in which our loudest signal can vanish was opened several times a day.
    With two instances the health endpoint showed whichever half the check landed
    on.

    Fire-and-forget on purpose: an alert is raised from inside a live call, and
    waiting for a database write to say "the clinic was not paged" would make a
    bad moment slower. The in-memory copy stays either way — an alert about the
    database being unreachable cannot be stored in the database, and that is
    exactly when it is worth having.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (a script, a sync test) — the buffer is the record
    task = loop.create_task(_write(kind, detail))
    _WRITES.add(task)
    task.add_done_callback(_WRITES.discard)


async def _write(kind: str, detail: str) -> None:
    with contextlib.suppress(Exception):  # never let telemetry break a call
        from app.db import platform_session_factory
        from app.models.alert_event import AlertEvent

        async with platform_session_factory() as session:
            session.add(AlertEvent(id=uuid.uuid4(), kind=kind, detail=detail))
            await session.commit()


async def recent_alerts_stored(*, window_s: int = _WINDOW_S) -> dict | None:
    """The last hour from the database, or None when it cannot be read.

    None means "fall back to the buffer" rather than "no alerts": reporting quiet
    because the database is unreachable is the failure this whole file exists to
    prevent, reproduced one level up.
    """
    try:
        from sqlalchemy import select

        from app.db import platform_session_factory
        from app.models.alert_event import AlertEvent

        cutoff = datetime.now(tz=UTC) - timedelta(seconds=window_s)
        async with platform_session_factory() as session:
            rows = (await session.execute(
                select(AlertEvent.kind, AlertEvent.detail)
                .where(AlertEvent.created_at >= cutoff)
                .order_by(AlertEvent.created_at)
            )).all()
    except Exception:  # noqa: BLE001 — the caller falls back to memory
        return None
    by_kind: dict[str, int] = {}
    for kind, _detail in rows:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    last = rows[-1] if rows else None
    return {
        "count_last_hour": len(rows),
        "by_kind": by_kind,
        "last_kind": last[0] if last else None,
        "last_detail": last[1] if last else None,
    }


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
