from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class WaitlistSummary(BaseModel):
    id: str
    call_id: str | None
    patient_name_redacted: str | None
    phone_last4: str | None
    procedure_type: str | None
    preferred_date: str | None
    preferred_time_window: str | None
    notes: str | None
    status: str
    notified_at: datetime | None
    created_at: datetime


class WaitlistListResponse(BaseModel):
    entries: list[WaitlistSummary]
    total: int
    has_more: bool
    waiting: int


class WaitlistStatusUpdate(BaseModel):
    status: str  # waiting | notified | booked | removed
