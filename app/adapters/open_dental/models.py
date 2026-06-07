"""PMS-neutral models mirroring Open Dental API shapes.

These stay PMS-neutral so the rest of the app (voice flow, bookings table) never
imports Open Dental specifics — only the adapter does. Open Dental's native keys
(PatNum / AptNum / ProvNum / OpNum) ride along as ``pms_external_id`` /
``apt_num`` / ``prov_num`` / ``op_num`` so we can write back to the PMS later.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class PMSPatient(BaseModel):
    pms_external_id: str  # Open Dental PatNum (as string)
    first_name: str
    last_name: str
    phone: str
    email: str | None = None
    date_of_birth: date | None = None


class PMSProvider(BaseModel):
    """A dentist/hygienist. Open Dental needs a ProvNum to offer slots / book."""

    prov_num: str          # Open Dental ProvNum
    name: str              # display name (for the agent to speak)
    abbreviation: str | None = None


class PMSOperatory(BaseModel):
    """A chair/room. Open Dental needs an OpNum to offer slots / book."""

    op_num: str            # Open Dental OpNum
    name: str
    prov_num: str | None = None  # default provider for this operatory, if set


class AvailableSlot(BaseModel):
    date: str              # ISO date, e.g. "2026-06-05"
    time: str              # "HH:MM" 24h local
    provider: str          # display name (for the agent to speak)
    # PMS keys needed to actually BOOK this slot — Open Dental won't create an
    # appointment without a provider + operatory. Optional so the mock can omit
    # them; the real client always fills them.
    prov_num: str | None = None
    op_num: str | None = None


class CreatedAppointment(BaseModel):
    pms_external_id: str   # patient PatNum (kept for back-reference)
    apt_num: str | None = None  # Open Dental AptNum — the real appointment id
    appointment_at: str    # ISO datetime
    duration_minutes: int
    procedure_type: str
    provider_name: str
