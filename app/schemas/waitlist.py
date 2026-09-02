from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class WaitlistSummary(BaseModel):
    id: str
    call_id: str | None
    patient_name_redacted: str | None
    phone_last4: str | None
    # The clinic's own patients, on the clinic's own screen. Masking these was a
    # habit carried from our cross-tenant admin views, where it belongs — here it
    # made the feature useless: a callback request you cannot call back, a
    # waitlist you cannot fill, a booking you cannot ring about. The practice is
    # the covered entity for these people; withholding their number from the
    # front desk protects nobody and stops the work.
    patient_name: str | None = None
    patient_phone: str | None = None
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
