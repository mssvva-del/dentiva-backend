from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CallbackSummary(BaseModel):
    id: str
    call_id: str | None
    patient_name_redacted: str | None
    phone_last4: str | None
    reason: str | None
    urgent: bool
    status: str
    created_at: datetime


class CallbackListResponse(BaseModel):
    callbacks: list[CallbackSummary]
    total: int
    has_more: bool
    pending_urgent: int


class CallbackStatusUpdate(BaseModel):
    status: str  # pending | handled | dismissed
