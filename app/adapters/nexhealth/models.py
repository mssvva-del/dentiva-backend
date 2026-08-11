"""PMS-neutral DTO for a reactivation pull record.

The Reactivation Engine needs more about a patient than booking does: when they
last came, when they're due, how much un-done treatment they have, and what
language they speak. This DTO is what every reactivation data source (NexHealth
now, others later) returns — the engine never sees PMS-specific JSON.

All the "value" fields default to safe zeros/None so a real PMS returning dirty
or partial data can't crash segmentation/scoring — a missing field just means
"unknown", handled downstream.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class PMSReactivationRecord(BaseModel):
    pms_external_id: str          # PMS patient id (NexHealth patient id as string)
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    email: str | None = None
    # EN/ES preference from the PMS; defaults 'en' when the PMS doesn't say.
    preferred_language: str = "en"
    # Segmentation inputs (block 3):
    last_visit_date: date | None = None       # → "lapsed" if long ago
    recall_due_date: date | None = None        # → "overdue_recall" if past
    # Value-scoring inputs (block 4), in cents to avoid float money:
    treatment_plan_value_cents: int = 0        # unscheduled/undone treatment → "dropped_treatment"
    balance_cents: int = 0
    # Consent/contactability from the PMS — never contact a patient the PMS marks
    # as opted-out. Defaults True; the campaign layer also re-checks our own
    # sms_opt_out + TCPA quiet hours.
    contactable: bool = True


class NexHealthSlot(BaseModel):
    """An open appointment slot offered by the PMS (for anti-double-book check)."""

    start_time: str            # ISO datetime
    provider_id: str
    operatory_id: str | None = None


class NexHealthAppointment(BaseModel):
    """The PMS's record of an appointment we created (write-back result)."""

    appointment_id: str        # NexHealth appointment id → stored on our booking
    start_time: str


# PMS-neutral aliases. These DTOs were named after the first PMS we spoke to and
# are not specific to it — Kolla returns the same two shapes. New adapters use
# the neutral names; the old ones stay so nothing has to be renamed at once.
PmsSlot = NexHealthSlot
PmsAppointment = NexHealthAppointment
