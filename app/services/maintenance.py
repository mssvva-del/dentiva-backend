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

from sqlalchemy import delete, select, update

import app.db as app_db
from app.config import get_settings
from app.db import set_tenant
from app.models.call import Call
from app.models.practice import Practice
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


async def scrub_expired_transcripts(
    *, now: datetime | None = None, retention_days: int | None = None
) -> int:
    """PHI data-minimization: null out transcripts + recording paths on calls older
    than the retention window, keeping the row's metadata (outcome, duration, timing)
    for analytics/billing. Returns rows scrubbed. 0 retention days disables it.

    Runs per-practice (calls are RLS-scoped, so a cross-tenant UPDATE as the app
    role would silently touch only one tenant) — set_tenant per practice.

    NOTE: this nulls the recording_path POINTER. When recordings live in external
    object storage (S3 signed URLs in prod), the audio file itself must be expired
    by a bucket lifecycle policy or an enqueued delete — nulling the column alone
    does not remove the stored PHI. Tracked as a prod follow-up."""
    now = now or datetime.now(tz=UTC)
    days = (
        retention_days if retention_days is not None
        else get_settings().call_transcript_retention_days
    )
    if not days or days <= 0:
        return 0
    cutoff = now - timedelta(days=days)

    scrubbed = 0
    async with app_db.async_session_factory() as session:
        practice_ids = (await session.execute(select(Practice.id))).scalars().all()
    for pid in practice_ids:
        async with app_db.async_session_factory() as session:
            await set_tenant(session, pid)
            result = await session.execute(
                update(Call)
                .where(
                    Call.practice_id == pid,
                    Call.started_at < cutoff,
                    (Call.transcript_jsonb.isnot(None)) | (Call.recording_path.isnot(None)),
                )
                .values(transcript_jsonb=None, recording_path=None)
            )
            await session.commit()
            scrubbed += result.rowcount or 0
    return scrubbed


async def maintenance_loop() -> None:
    """Run maintenance forever on a fixed interval."""
    interval = get_settings().maintenance_interval_seconds
    logger.info("maintenance loop started (every %ss)", interval)
    while True:
        try:
            removed = await prune_processed_events()
            if removed:
                logger.info("maintenance: pruned %s processed_webhook_events", removed)
            scrubbed = await scrub_expired_transcripts()
            if scrubbed:
                logger.info("maintenance: scrubbed PHI on %s expired calls", scrubbed)
        except asyncio.CancelledError:
            logger.info("maintenance loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("maintenance iteration failed; will retry next tick")
        await asyncio.sleep(interval)
