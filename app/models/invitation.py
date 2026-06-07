from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Invitation(UUIDPKMixin, TimestampMixin, Base):
    """A pending team invitation (Platform Iter 1, Phase B3).

    Lifecycle: pending → accepted (person joined via Clerk) | revoked (owner
    cancelled) | expired. Tenant-isolated by RLS on practice_id.
    """

    __tablename__ = "invitations"

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # Clinic role the invitee will get on acceptance: owner|manager|staff|viewer.
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | accepted | revoked | expired
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # Clerk's organization-invitation id, when created via the Clerk API.
    clerk_invitation_id: Mapped[str | None] = mapped_column(Text)
    # The user (id) who sent the invite, for audit.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
