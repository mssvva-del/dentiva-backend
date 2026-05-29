"""Real Open Dental API client — STUB for Iter 2.

Do NOT implement in weekend mode. Left here so the factory has a target.
"""

from __future__ import annotations

from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSPatient,
)


class OpenDentalClient(PMSAdapter):
    async def get_patient(self, pms_external_id: str) -> PMSPatient | None:
        raise NotImplementedError("Real Open Dental integration lands in Iter 2.")

    async def get_patient_by_phone(self, phone: str) -> PMSPatient | None:
        raise NotImplementedError("Real Open Dental integration lands in Iter 2.")

    async def check_availability(
        self, date_from: str, date_to: str, preferred_window: str | None = None
    ) -> list[AvailableSlot]:
        raise NotImplementedError("Real Open Dental integration lands in Iter 2.")

    async def create_appointment(
        self,
        pms_external_id: str,
        appointment_at: str,
        procedure_type: str,
        duration_minutes: int = 60,
    ) -> CreatedAppointment:
        raise NotImplementedError("Real Open Dental integration lands in Iter 2.")
