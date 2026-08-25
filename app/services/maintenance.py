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
from app.observability.alerts import record_alert
from app.services.worker_lock import advisory_tick_lock

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


async def link_synced_locations() -> int:
    """Connect a clinic to its own calendar the moment NexHealth finishes syncing.

    The clinic's setup screen promises "this page will say connected on its own —
    you do not need to tell us". That was only true if a human was watching the
    admin panel and pasted the location id in; nobody automated the last inch, so
    the promise depended on us noticing. This is that inch.

    A practice is WAITING when it has an installer key (someone ran, or is
    running, the Synchronizer) and no location id yet. A location is UNCLAIMED
    when no practice already points at it.

    Links only the unambiguous case: exactly one waiting practice, exactly one
    unclaimed location. Anything else pages an admin to pick instead of guessing.
    Guessing here means reading one clinic's calendar aloud to another clinic's
    patient and writing an appointment into the wrong practice — matching two
    similar names by string distance is not worth that. Two clinics installing
    on the same day is normal; it just means a human picks, once.

    Returns the number of practices linked (0 or 1 today, by construction).
    """
    from app.adapters.nexhealth.client import NexHealthClient

    if not get_settings().nexhealth_api_key:
        return 0

    async with app_db.platform_session_factory() as session:
        practices = (await session.execute(select(Practice))).scalars().all()
        waiting, claimed = [], set()
        for p in practices:
            creds = p.pms_credentials if isinstance(p.pms_credentials, dict) else {}
            location_id = str(creds.get("location_id") or "").strip()
            if location_id:
                claimed.add(location_id)
            elif str(creds.get("product_key") or "").strip():
                waiting.append(p)

        if not waiting:
            return 0

        locations = await NexHealthClient().list_locations()
        unclaimed = [
            loc for loc in locations
            if str(loc.get("id") or "").strip()
            and str(loc.get("id")).strip() not in claimed
        ]
        if not unclaimed:
            # The normal state while the clinic's IT has not run the installer.
            return 0

        if len(waiting) > 1 or len(unclaimed) > 1:
            record_alert(
                "pms_autolink_ambiguous",
                f"waiting={len(waiting)} unclaimed={len(unclaimed)} — link manually",
            )
            return 0

        practice, location = waiting[0], unclaimed[0]
        # Merge: the installer key stays. It is what the clinic reads off its own
        # screen, and a re-run of the installer needs it after this point too.
        practice.pms_credentials = {
            **(practice.pms_credentials or {}),
            "location_id": str(location["id"]).strip(),
        }
        await session.commit()

    logger.info("maintenance: auto-linked practice %s to its PMS location", practice.id)
    return 1


async def check_billing_catalog() -> int:
    """Page if Stripe would charge something other than what we sell.

    Configuration bugs, not code bugs: a live key left beside test price ids, or
    a price change in plans.py that nobody synced to Stripe. Both are invisible
    until a clinic is at the checkout with a card in hand, so this asks Stripe
    before anyone does. Returns the number of problems found.
    """
    from app.billing.catalog_check import verify_catalog

    try:
        problems = await verify_catalog()
    except Exception:  # noqa: BLE001 — Stripe being down is not a catalog fault
        logger.warning("maintenance: could not verify the Stripe catalog")
        return 0
    for problem in problems:
        record_alert("billing_catalog_mismatch", problem)
    return len(problems)


async def maintenance_loop() -> None:
    """Run maintenance forever on a fixed interval."""
    interval = get_settings().maintenance_interval_seconds
    logger.info("maintenance loop started (every %ss)", interval)
    while True:
        try:
            # Idempotent deletes, but run once per tick across instances anyway.
            async with advisory_tick_lock("maintenance") as leader:
                if leader:
                    removed = await prune_processed_events()
                    if removed:
                        logger.info("maintenance: pruned %s processed_webhook_events", removed)
                    scrubbed = await scrub_expired_transcripts()
                    if scrubbed:
                        logger.info("maintenance: scrubbed PHI on %s expired calls", scrubbed)
                    await link_synced_locations()
                    await check_billing_catalog()
        except asyncio.CancelledError:
            logger.info("maintenance loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("maintenance iteration failed; will retry next tick")
        await asyncio.sleep(interval)
