from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Call(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_practice_started", "practice_id", "started_at"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    retell_call_id: Mapped[str | None] = mapped_column(Text, unique=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)  # inbound | outbound
    from_number: Mapped[str] = mapped_column(Text, nullable=False)
    to_number: Mapped[str] = mapped_column(Text, nullable=False)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    recording_path: Mapped[str | None] = mapped_column(Text)
    transcript_jsonb: Mapped[dict | list | None] = mapped_column(JSONB)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    language_detected: Mapped[str | None] = mapped_column(Text)
    call_intent: Mapped[str | None] = mapped_column(Text)
    patient_sentiment: Mapped[str | None] = mapped_column(Text)
    escalation_needed: Mapped[bool | None] = mapped_column(Boolean)
    hipaa_compliant: Mapped[bool | None] = mapped_column(Boolean)
    # Programmatic emergency lock (persisted so it survives backend restarts and
    # spans every webhook/tool call within one phone call). When True, the Retell
    # tool router physically refuses check_availability / book_appointment — the
    # agent CANNOT schedule during an active emergency, regardless of the prompt.
    emergency_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
