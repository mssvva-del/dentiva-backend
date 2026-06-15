from __future__ import annotations

from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class ProcessedWebhookEvent(UUIDPKMixin, TimestampMixin, Base):
    """Generic webhook-event dedup ledger (idempotency).

    Providers (Twilio, …) redeliver webhooks on timeout. Before acting on an
    event we record (source, event_id); a duplicate delivery hits the unique
    constraint and is skipped. Not RLS-protected — it's infra-level dedup keyed by
    the provider's own id, written before any tenant context exists.
    """

    __tablename__ = "processed_webhook_events"
    __table_args__ = (
        UniqueConstraint("source", "event_id", name="uq_processed_event"),
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)    # e.g. 'twilio_sms'
    event_id: Mapped[str] = mapped_column(Text, nullable=False)  # provider's unique id
