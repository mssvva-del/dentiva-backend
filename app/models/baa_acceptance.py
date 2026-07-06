from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class BaaAcceptance(UUIDPKMixin, TimestampMixin, Base):
    """A clinic's click-through acceptance of a specific Terms + BAA version.

    Legal record (ESIGN e-signature): who accepted, which document version, when
    (created_at), and the IP + user-agent captured from the request. Append-only
    by convention — a new version acceptance is a new row, so the full consent
    history is auditable. Tenant-scoped (RLS): a clinic only ever sees its own.
    """

    __tablename__ = "baa_acceptances"
    # One acceptance per (practice, version) — matches the migration's UNIQUE index
    # so ON CONFLICT works under create_all (tests) and Alembic (prod) alike.
    __table_args__ = (
        Index("uq_baa_acceptances_practice_version",
              "practice_id", "document_version", unique=True),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    document_version: Mapped[str] = mapped_column(Text, nullable=False)
    signer_name: Mapped[str] = mapped_column(Text, nullable=False)
    signer_title: Mapped[str] = mapped_column(Text, nullable=False)
    # Captured server-side from the request — evidence for the e-signature.
    signer_ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    # The user (Clerk) who clicked accept, for the audit trail.
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
