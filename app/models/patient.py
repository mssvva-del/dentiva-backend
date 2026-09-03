from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Text, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin
from app.models.types import EncryptedString
from app.utils.crypto import phone_hmac as _compute_phone_hmac


class Patient(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("practice_id", "pms_external_id", name="uq_patient_practice_pms"),
        # Matches the tenant-scoped phone lookup (practice_id AND phone_hmac).
        # Non-unique: family members legitimately share a phone in one practice.
        Index("ix_patients_practice_phone_hmac", "practice_id", "phone_hmac"),
    )

    # WHY practice_id is NON-NULL: it's the tenant key the RLS policy filters on.
    # A null here would be invisible to every tenant (and a hole in isolation), so
    # the column must always be set — never insert a patient without a practice.
    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    pms_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    # WHY EncryptedString (not Text): these are PHI. Stored as bytea ciphertext
    # (Fernet) so a DB dump/backup never exposes names/phone/DOB in cleartext.
    # Encrypted PII columns (bytea under the hood).
    first_name: Mapped[str | None] = mapped_column(EncryptedString)
    last_name: Mapped[str | None] = mapped_column(EncryptedString)
    phone: Mapped[str | None] = mapped_column(EncryptedString)
    # Searchable sidecar for the encrypted phone: a deterministic HMAC that IS
    # indexable (see composite index in __table_args__), so lookups by number are a
    # single indexed query instead of an O(n) decrypt-every-row scan. Kept in sync
    # automatically by the event listeners below — never set it by hand. NOT PHI
    # (keyed one-way hash).
    phone_hmac: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(EncryptedString)
    # DOB stored encrypted as ISO string (date is PHI).
    date_of_birth: Mapped[str | None] = mapped_column(EncryptedString)
    # About the person, not one visit: allergies, "always brings her daughter",
    # "pay by card only". The front desk types this after meeting them, and it
    # outlives every appointment. Encrypted for the same reason the name is.
    notes: Mapped[str | None] = mapped_column(EncryptedString)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SMS opt-out (TCPA compliance). Set when a patient texts STOP; cleared on
    # START. All outbound SMS senders skip opted-out patients.
    sms_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Preferred conversation language (EN/ES) for voice + SMS, honored by the
    # receptionist and reactivation outreach. WHY plain Text, not EncryptedString:
    # it's a preference, not PHI — and we need it in cleartext to filter/segment
    # campaigns by language in SQL. Defaults to 'en'; set from call language
    # detection or PMS pull.
    preferred_language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="en", default="en"
    )


# Keep phone_hmac in lockstep with phone on every insert/update, everywhere —
# no call site has to remember to set it. At flush time target.phone is the
# plaintext (EncryptedString encrypts on the way to the DB), so we can hash it.
@event.listens_for(Patient, "before_insert")
@event.listens_for(Patient, "before_update")
def _sync_phone_hmac(_mapper, _connection, target: Patient) -> None:
    target.phone_hmac = _compute_phone_hmac(target.phone)
