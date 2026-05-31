from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin
from app.models.types import EncryptedString


class Patient(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("practice_id", "pms_external_id", name="uq_patient_practice_pms"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    pms_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Encrypted PII columns (bytea under the hood).
    first_name: Mapped[str | None] = mapped_column(EncryptedString)
    last_name: Mapped[str | None] = mapped_column(EncryptedString)
    phone: Mapped[str | None] = mapped_column(EncryptedString)
    email: Mapped[str | None] = mapped_column(EncryptedString)
    # DOB stored encrypted as ISO string (date is PHI).
    date_of_birth: Mapped[str | None] = mapped_column(EncryptedString)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SMS opt-out (TCPA compliance). Set when a patient texts STOP; cleared on
    # START. All outbound SMS senders skip opted-out patients.
    sms_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
