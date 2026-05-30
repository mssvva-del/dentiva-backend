from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class BookingSummary(BaseModel):
    id: str
    patient_name_redacted: str | None
    patient_id: str
    appointment_at: datetime
    duration_minutes: int
    procedure_type: str | None
    provider_name: str | None
    status: str
    source: str
    source_call_id: str | None
    created_at: datetime


class BookingListResponse(BaseModel):
    bookings: list[BookingSummary]
    total: int


class DashboardToday(BaseModel):
    calls_today: int
    calls_answered_by_ai: int
    calls_missed: int
    bookings_made_today: int
    upcoming_appointments_today: int


class BriefingResponse(BaseModel):
    text: str
    stats: DashboardToday
    peak_hours: list[dict]
    generated_at: datetime
    ai_generated: bool
