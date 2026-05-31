from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class WaitlistEntry(UUIDPKMixin, TimestampMixin, Base):
    """A patient who wants an earlier slot than what was available.

    Created by the voice agent's ``join_waitlist`` tool when the caller can't
    find a time that works. When an appointment is later cancelled, the
    cancellation handler "backfills" by notifying the oldest waiting entry that
    a slot just opened up — turning a cancellation into a rebooking opportunity.

    Patient PII lives on the ``patients`` table (encrypted); this row only
    references ``patient_id``, so no PII is duplicated here. The free-text
    preference fields are operational notes kept in plaintext within the tenant.
    """

    __tablename__ = "waitlist_entries"
    __table_args__ = (
        Index("ix_waitlist_practice_status_created", "practice_id", "status", "created_at"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id"), nullable=False
    )
    # Optional link to the call that generated this entry.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id")
    )
    # What the patient is waiting for (operational free text).
    procedure_type: Mapped[str | None] = mapped_column(Text)
    preferred_date: Mapped[str | None] = mapped_column(Text)
    preferred_time_window: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # waiting | notified | booked | removed
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="waiting", default="waiting"
    )
    # Stamped when the backfill notification SMS is sent.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
