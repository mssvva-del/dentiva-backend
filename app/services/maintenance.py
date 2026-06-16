"""Background maintenance — keep infra tables from growing unbounded.

Currently prunes the webhook-dedup ledger (``processed_webhook_events``). That
table gains one row per inbound provider webhook (e.g. every inbound SMS) and is
only consulted to reject DUPLICATE redeliveries. A provider redelivers within
hours at most, so rows older than the TTL can never match a real redelivery and
are safe to delete.

Runs two ways (mirroring call_sync/reminders):
  * one-shot via :func:`prune_processed_events` (tests/scripts), and
  * a periodic loop via :func:`maintenance_loop`, started from the FastAPI
    lifespan when ``MAINTENANCE_ENABLED=true``.

``processed_webhook_events`` is non-RLS infra, so no tenant binding is needed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

import app.db as app_db
from app.config import get_settings
from app.models.processed_webhook_event import ProcessedWebhookEvent

logger = logging.getLogger(__name__)


async def prune_processed_events(
    *, now: datetime | None = None, ttl_days: int | None = None
) -> int:
    """Delete dedup-ledger rows older than the TTL. Returns rows removed."""
    now = now or datetime.now(tz=UTC)
    ttl_days = ttl_days if ttl_days is not None else get_settings().processed_event_ttl_days
    cutoff = now - timedelta(days=ttl_days)
    async with app_db.async_session_factory() as session:
        result = await session.execute(
            delete(ProcessedWebhookEvent).where(
                ProcessedWebhookEvent.created_at < cutoff
            )
        )
        await session.commit()
    return result.rowcount or 0


async def maintenance_loop() -> None:
    """Run maintenance forever on a fixed interval."""
    interval = get_settings().maintenance_interval_seconds
    logger.info("maintenance loop started (every %ss)", interval)
    while True:
        try:
            removed = await prune_processed_events()
            if removed:
                logger.info("maintenance: pruned %s processed_webhook_events", removed)
        except asyncio.CancelledError:
            logger.info("maintenance loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("maintenance iteration failed; will retry next tick")
        await asyncio.sleep(interval)
