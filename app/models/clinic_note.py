from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class ClinicNote(UUIDPKMixin, TimestampMixin, Base):
    """An internal Dentiva-staff note about a clinic (account CRM).

    NOT patient data — this is OUR sales/support commentary on the account, so
    like `leads` it is cross-tenant business data with NO RLS (only internal
    staff reach it, via the audited /api/admin path). author_email is snapshotted
    for display so the note still shows who wrote it if the user row changes.
    """

    __tablename__ = "clinic_notes"
    __table_args__ = (
        Index("ix_clinic_notes_practice_created", "practice_id", "created_at"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    author_email: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
