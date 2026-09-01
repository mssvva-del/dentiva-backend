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
import re
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
    """Connect clinics to their own calendars as their installers finish.

    The clinic's setup screen promises "this page will say connected on its own —
    you do not need to tell us". That was only true if a human was watching the
    admin panel and pasted the location id in.

    A practice is WAITING when it has an installer key and no location id yet. A
    location is UNCLAIMED when no practice already points at it. Waiting and
    unclaimed are matched by NAME, and only where that name is unambiguous on
    BOTH sides — both names are written by the same people, so this is a key
    rather than a similarity score.

    Anything the names cannot settle one-to-one is left for a human and reported
    once per tick. Guessing here means reading one clinic's calendar aloud to
    another clinic's patient and writing an appointment into the wrong practice;
    a near-match is not a smaller version of that mistake.

    Returns the number of practices linked.
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

        # ── Matching ────────────────────────────────────────────────────
        #
        # The original rule was "exactly one waiting, exactly one unclaimed".
        # Safe, and useless the moment a group practice rolls out: with a fleet
        # installing over the same fortnight it never fires once, and every
        # clinic waits for a human to paste an id.
        #
        # So: match on the NAME, and only where the name is unambiguous on BOTH
        # sides. Both names come from the same people — the group names the
        # location in NexHealth as they name it to us — so this is a real key,
        # not a similarity score. Two clinics genuinely called the same thing
        # (a brand with an office in two towns) match nothing and fall back to a
        # human, which is the right answer for a case a machine cannot tell
        # apart from a mix-up.
        #
        # Nothing here is fuzzy on purpose. A near-match that links one clinic
        # to another's calendar is not a smaller version of the same mistake.
        by_name: dict[str, list] = {}
        for loc in unclaimed:
            by_name.setdefault(_norm_name(loc.get("name")), []).append(loc)
        waiting_by_name: dict[str, list] = {}
        for p in waiting:
            waiting_by_name.setdefault(_norm_name(p.name), []).append(p)

        linked: list[str] = []
        for name, practices in waiting_by_name.items():
            candidates = by_name.get(name) or []
            if len(practices) != 1 or len(candidates) != 1 or not name:
                continue
            practice = practices[0]
            # Merge: the installer key stays. It is what the clinic reads off
            # its own screen, and a re-run of the installer needs it after this.
            practice.pms_credentials = {
                **(practice.pms_credentials or {}),
                "location_id": str(candidates[0]["id"]).strip(),
            }
            linked.append(practice.name)

        # Whatever the names could not settle. Reported once per tick with the
        # counts, because an operator needs to know a queue exists — not to be
        # paged once per unmatched clinic every hour.
        unmatched_practices = len(waiting) - len(linked)
        unmatched_locations = len(unclaimed) - len(linked)
        if unmatched_practices and unmatched_locations:
            record_alert(
                "pms_autolink_needs_a_human",
                f"waiting={unmatched_practices} unclaimed={unmatched_locations} "
                "— names did not match one-to-one; link these by hand",
            )

        if not linked:
            return 0
        await session.commit()

    logger.info("maintenance: auto-linked %s practice(s): %s",
                len(linked), ", ".join(sorted(linked)))
    return len(linked)


def _norm_name(value: str | None) -> str:
    """A practice name reduced to what two systems can agree on.

    Case, punctuation and the words a group adds in one place and not the other
    ("LLC", "PC", "Dental"). Deliberately conservative: it removes noise, it
    never guesses that two different names are the same clinic.
    """
    text = _NAME_PUNCT.sub(" ", (value or "").strip().lower())
    words = [w for w in text.split() if w not in _NAME_NOISE]
    return " ".join(words)


_NAME_PUNCT = re.compile(r"[^a-z0-9 ]+")
_NAME_NOISE = frozenset({"llc", "pc", "pa", "inc", "the", "dds", "dmd"})



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


async def billing_catalog_loop() -> None:
    """Its own loop, on its own interval.

    This ran on the daily maintenance tick, which made the alert almost useless
    in practice: it says our prices and Stripe's disagree, somebody corrects the
    configuration in minutes, and then has no way to confirm the correction for
    twenty-four hours. An alert nobody can close is one people learn to ignore.

    Eight Stripe reads an hour is nothing, and the failure it catches — a clinic
    billed a price we do not sell — is expensive and silent.
    """
    interval = get_settings().billing_catalog_interval_seconds
    logger.info("billing catalog loop started (every %ss)", interval)
    while True:
        try:
            async with advisory_tick_lock("billing_catalog") as leader:
                if leader:
                    problems = await check_billing_catalog()
                    if problems:
                        logger.warning("billing catalog: %s problem(s)", problems)
        except asyncio.CancelledError:
            logger.info("billing catalog loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("billing catalog check failed; will retry next tick")
        await asyncio.sleep(interval)


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
        except asyncio.CancelledError:
            logger.info("maintenance loop cancelled — stopping")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die on a transient error
            logger.exception("maintenance iteration failed; will retry next tick")
        await asyncio.sleep(interval)
