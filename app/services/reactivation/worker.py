"""Reactivation worker loop (Phase 1 — the "queue").

The reactivation TARGETS table is already a durable job queue: every target
carries ``next_touch_at`` (when its next touch is due) and ``select_due_targets``
reads the ready ones, highest-value first. So we don't need Redis/Celery — we
need a thin loop that, on an interval, drains the due touches per practice. This
is the wrapper over ``process_due_targets`` (which does one practice's tick).

Durable + restart-safe: state lives in Postgres, not in memory. If the process
dies mid-run, nothing is lost — un-processed targets are still due next tick, and
each touch is recorded transactionally.

Gated OFF by default (REACTIVATION_ENABLED) so it never auto-dials until the
live-loop (real number + PMS) is switched on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

import app.db as app_db
from app.config import get_settings
from app.models.practice import Practice
from app.services.reactivation.outreach import process_due_targets
from app.services.reactivation.voice import get_voice_sender

logger = logging.getLogger("dentiva.reactivation.worker")


async def run_reactivation_tick(
    session_factory=None, *, now: datetime | None = None  # noqa: ANN001
) -> dict:
    """One worker pass over ALL practices. Each practice gets its own session
    (set_tenant is connection-scoped). Returns an aggregate summary. Practices
    with no running campaign / nothing due are cheap no-ops."""
    session_factory = session_factory or app_db.async_session_factory
    # Resolve the voice sender once per tick (None until a real number is set →
    # voice touches defer; SMS still flows).
    voice_sender = get_voice_sender()

    async with session_factory() as session:
        practice_ids = (await session.execute(select(Practice.id))).scalars().all()

    totals = {"processed": 0, "sent": 0, "failed": 0, "deferred": 0, "opted_out": 0}
    for pid in practice_ids:
        async with session_factory() as session:
            s = await process_due_targets(
                session, pid, now=now, voice_sender=voice_sender
            )
        for k in totals:
            totals[k] += s.get(k, 0)
    return totals


async def reactivation_worker_loop() -> None:
    """Drain due reactivation touches forever on a fixed interval."""
    interval = get_settings().reactivation_interval_seconds
    logger.info("reactivation worker loop started (every %ss)", interval)
    while True:
        try:
            result = await run_reactivation_tick()
            if result["processed"] or result["deferred"]:
                logger.info("reactivation worker: %s", result)
        except asyncio.CancelledError:
            logger.info("reactivation worker loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("reactivation worker iteration failed; will retry next tick")
        await asyncio.sleep(interval)
