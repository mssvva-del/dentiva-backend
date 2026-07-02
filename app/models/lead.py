from __future__ import annotations

from sqlalchemy import Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Lead(UUIDPKMixin, TimestampMixin, Base):
    """A sales lead — a dental practice that submitted the marketing-site demo form.

    OUR business data, not clinic PHI: it exists BEFORE any practice/tenant, so it has
    no practice_id and no RLS. Read/managed only by Dentiva admin staff with
    MANAGE_LEADS (deny-by-default). Contact fields are B2B prospect info (not patient
    health data), stored as plain text.
    """

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_status_created", "status", "created_at"),
    )

    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    clinic_name: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    # Where the lead came from (site demo form, referral, etc). Default 'site'.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="site")
    # Sales pipeline stage: new | contacted | qualified | won | lost.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    # Internal sales notes (admin-only).
    notes: Mapped[str | None] = mapped_column(Text)
