"""Maintenance: prune the webhook-dedup ledger by TTL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.processed_webhook_event import ProcessedWebhookEvent
from app.services.maintenance import prune_processed_events


async def test_prune_deletes_old_keeps_recent(db_session):
    now = datetime.now(tz=UTC)
    db_session.add_all([
        ProcessedWebhookEvent(
            id=uuid.uuid4(), source="twilio_sms", event_id="OLD",
            created_at=now - timedelta(days=100),
        ),
        ProcessedWebhookEvent(
            id=uuid.uuid4(), source="twilio_sms", event_id="RECENT",
            created_at=now - timedelta(days=1),
        ),
    ])
    await db_session.commit()

    removed = await prune_processed_events(now=now, ttl_days=90)
    assert removed == 1

    remaining = (
        await db_session.execute(select(ProcessedWebhookEvent.event_id))
    ).scalars().all()
    assert "RECENT" in remaining
    assert "OLD" not in remaining


async def test_prune_noop_on_empty(db_session):
    assert await prune_processed_events(ttl_days=90) == 0
