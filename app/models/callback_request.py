from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin
from app.models.types import EncryptedString


class CallbackRequest(UUIDPKMixin, TimestampMixin, Base):
    """A request for a human to call the patient back.

    Created by the voice agent's create_callback_request tool (and the only
    scheduling-adjacent action allowed during an active emergency). Persisted so
    the practice sees pending callbacks on the dashboard — urgent ones first.
    Patient identifiers are encrypted at rest like the patients table; the
    callback reason is operational free text kept in plaintext within the
    tenant.
    """

    __tablename__ = "callback_requests"
    __table_args__ = (
        Index("ix_callbacks_practice_created", "practice_id", "created_at"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    # Optional link to the call that generated this callback.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id")
    )
    # Encrypted PII (bytea under the hood).
    patient_first_name: Mapped[str | None] = mapped_column(EncryptedString)
    phone: Mapped[str | None] = mapped_column(EncryptedString)
    # Operational free text (why the patient should be called back).
    reason: Mapped[str | None] = mapped_column(Text)
    # True when the caller described an emergency / urgent situation.
    urgent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # pending | handled | dismissed
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending", default="pending"
    )
