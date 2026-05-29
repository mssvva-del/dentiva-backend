from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CallSummary(BaseModel):
    id: str
    direction: str
    from_number: str
    to_number: str
    started_at: datetime
    duration_seconds: int | None
    status: str
    patient_name_redacted: str | None
    patient_id: str | None
    outcome: str | None
    booking_id: str | None
    transcript_available: bool


class CallListResponse(BaseModel):
    calls: list[CallSummary]
    total: int
    has_more: bool
