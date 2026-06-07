from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class FeatureFlag(UUIDPKMixin, TimestampMixin, Base):
    """A feature toggle (Platform Iter 1, Phase E — admin feature flags).

    practice_id NULL = a GLOBAL flag (applies to everyone); set = an override for
    one practice. Managed only from the Dentiva admin panel (MANAGE_FEATURE_FLAGS).
    Not RLS-protected: flags are operational config, read by admin and (later) by
    the app to gate features; per-practice scoping is by explicit query.
    """

    __tablename__ = "feature_flags"
    __table_args__ = (
        # A flag key is unique per scope (one global row, one row per practice).
        Index("ix_feature_flags_key", "flag_key"),
    )

    # NULL → global flag; otherwise the practice this override belongs to.
    practice_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    flag_key: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    description: Mapped[str | None] = mapped_column(Text)
