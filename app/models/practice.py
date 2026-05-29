from __future__ import annotations

from sqlalchemy import ARRAY, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Practice(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "practices"

    clerk_org_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="America/New_York"
    )
    phone_number: Mapped[str | None] = mapped_column(Text)
    pms_system: Mapped[str] = mapped_column(Text, nullable=False)
    pms_credentials_secret_key: Mapped[str | None] = mapped_column(Text)
    languages_enabled: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{en}"
    )
    business_hours: Mapped[dict] = mapped_column(JSONB, nullable=False)
    retell_agent_id: Mapped[str | None] = mapped_column(Text)
