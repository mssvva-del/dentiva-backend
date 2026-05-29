"""Pydantic models mirroring expected Open Dental API shapes (PMS-neutral)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class PMSPatient(BaseModel):
    pms_external_id: str
    first_name: str
    last_name: str
    phone: str
    email: str | None = None
    date_of_birth: date | None = None


class AvailableSlot(BaseModel):
    date: str  # ISO date, e.g. "2026-06-05"
    time: str  # "HH:MM" 24h local
    provider: str


class CreatedAppointment(BaseModel):
    pms_external_id: str
    appointment_at: str  # ISO datetime
    duration_minutes: int
    procedure_type: str
    provider_name: str
