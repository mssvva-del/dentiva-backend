"""An operational alert that survives the process that raised it.

Alerts lived in a ring buffer in memory. Two consequences, both quiet:

  * a redeploy erased them, and Railway redeploys on every merge — so the window
    in which the most important signal we have can vanish is opened several times
    a day,
  * with two instances the health endpoint reported whichever half the check
    happened to land on.

The one that matters is page_not_delivered_urgent_callback: the clinic was never
told about a caller who said they were bleeding, and the agent had already
promised them otherwise. That must not depend on which container answers, or on
nobody having deployed in the last hour.

Not tenant-scoped. An alert is about the platform, and the monitor that reads
these is not a clinic.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import UUIDPKMixin


class AlertEvent(UUIDPKMixin, Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        # Every read is "the last hour, newest first".
        Index("ix_alert_events_created_at", "created_at"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Codes, counts and ids only — never PHI. Same contract as record_alert.
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # NULL = nobody has dealt with it. Without this the admin list is every
    # problem ever reported, which reads as noise by week three — the same as
    # not having built the Report button at all.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A name, not a user id: the row outlives staff accounts, and "resolved by a
    # user that no longer exists" answers nothing.
    resolved_by: Mapped[str | None] = mapped_column(Text)
