from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import BOOKING_STATUSES, sql_in
from app.models.mixins import TimestampMixin, UUIDPKMixin
from app.models.types import EncryptedString


class Booking(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index("ix_bookings_practice_appt", "practice_id", "appointment_at"),
        # Double-book guards (partial, confirmed-only). Practice is a single
        # resource (one chair; provider is a label), so one confirmed booking per
        # (practice, slot); and one call → one confirmed booking.
        Index("uq_bookings_practice_slot_confirmed", "practice_id", "appointment_at",
              unique=True, postgresql_where=text("status = 'confirmed'")),
        Index("uq_bookings_source_call_confirmed", "source_call_id", unique=True,
              postgresql_where=text("status = 'confirmed' AND source_call_id IS NOT NULL")),
        CheckConstraint("status IN " + sql_in(BOOKING_STATUSES), name="ck_bookings_status"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id"), nullable=False
    )
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id")
    )
    pms_external_id: Mapped[str | None] = mapped_column(Text)
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    procedure_type: Mapped[str | None] = mapped_column(Text)
    provider_name: Mapped[str | None] = mapped_column(Text)
    # What the caller said that the row above cannot hold — which tooth, who is
    # driving them, that the last cleaning hurt. Written by the agent during the
    # call and editable by the front desk afterwards. Encrypted: it is the
    # patient's own words about their health.
    notes: Mapped[str | None] = mapped_column(EncryptedString)
    # What the practice's own system said when we last wrote to it. NULL when
    # the two calendars agree; otherwise the refusal, in their software's words.
    # Without it a cancellation their API rejects looks identical to one it took:
    # our screen says cancelled, their chair stays blocked, and nobody can tell.
    pms_sync_status: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="confirmed")
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="ai_call")
    # Appointment-reminder idempotency: set when each SMS reminder is sent so the
    # background scheduler never double-sends. NULL = not yet sent.
    reminder_24h_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_2h_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
