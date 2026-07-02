from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Text, event
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin
from app.models.types import EncryptedJSON, EncryptedString
from app.utils.crypto import phone_hmac as _compute_phone_hmac


class Call(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_practice_started", "practice_id", "started_at"),
        # Searchable index for the caller number (from_number is encrypted, so it
        # can't be queried directly — the dashboard call search matches this hash).
        Index("ix_calls_practice_caller_hmac", "practice_id", "caller_number_hmac"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    retell_call_id: Mapped[str | None] = mapped_column(Text, unique=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)  # inbound | outbound
    # Phone numbers are PHI — encrypted at rest (Fernet → bytea), like patient.phone.
    from_number: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    to_number: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    # Searchable deterministic hash of the caller number (from_number) — kept in
    # sync by the event listener below; the ONLY way to look up a call by number
    # now that from_number is encrypted. NOT PHI (keyed one-way hash).
    caller_number_hmac: Mapped[str | None] = mapped_column(Text)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    # Pointer to the call recording (external storage) — treated as PHI, encrypted.
    recording_path: Mapped[str | None] = mapped_column(EncryptedString)
    # WHY no encryption here but PII still inside: the transcript is free text and
    # callers may speak names/phone/DOB. Encrypted at rest (EncryptedJSON/Fernet →
    # bytea) so a DB dump never exposes full conversations; still masked on the way
    # OUT for display — see app/utils/redact.redact_transcript.
    transcript_jsonb: Mapped[dict | list | None] = mapped_column(EncryptedJSON)
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
    # Billing meter-once stamp: set when this call's minutes were added to usage.
    # NULL = not yet metered. Guards against double-counting when Retell redelivers
    # call_ended (webhooks are retried on timeout).
    usage_metered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Keep caller_number_hmac in sync with from_number on every write, so calls stay
# searchable by number even though from_number is now encrypted. At flush time
# target.from_number is the plaintext (EncryptedString encrypts on the way to DB).
@event.listens_for(Call, "before_insert")
@event.listens_for(Call, "before_update")
def _sync_caller_number_hmac(_mapper, _connection, target: Call) -> None:
    target.caller_number_hmac = _compute_phone_hmac(target.from_number)
