from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    clerk_user_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # NULLABLE since Platform Iter 1: internal Dentiva staff (is_internal=True)
    # belong to no clinic, so they have practice_id = NULL. Clinic users always
    # have one. Their Dentiva role lives in the `dentiva_staff` table.
    practice_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=True
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # Clinic role: 'owner' | 'manager' | 'staff' | 'viewer' (legacy 'admin' = owner).
    # For internal users this is unused; their power comes from dentiva_staff.role.
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # True = Dentiva internal team (admin world). Gates /admin access.
    is_internal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Account lifecycle: 'active' | 'invited' | 'disabled'.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active", default="active"
    )
