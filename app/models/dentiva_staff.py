from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class DentivaStaff(UUIDPKMixin, TimestampMixin, Base):
    """Internal Dentiva team member (admin world).

    One row per internal user (1:1 with a `users` row where is_internal=True).
    Holds the Dentiva role that drives admin-panel permissions. Provisioned
    MANUALLY (super_admin invites) — never via self-serve signup. NOT tenant-
    scoped: no practice_id, no RLS; access is guarded by is_internal + role checks
    in app/auth/admin.py, and every cross-clinic action is written to audit_logs.
    """

    __tablename__ = "dentiva_staff"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    # 'super_admin' | 'support' | 'sales' | 'finance' | 'engineer'  (see app/auth/permissions.py)
    role: Mapped[str] = mapped_column(Text, nullable=False)
