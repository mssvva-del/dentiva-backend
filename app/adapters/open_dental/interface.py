"""Abstract PMS adapter interface. Mock + real implementations conform to this."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSPatient,
)


class PMSAdapter(ABC):
    @abstractmethod
    async def get_patient(self, pms_external_id: str) -> PMSPatient | None: ...

    @abstractmethod
    async def get_patient_by_phone(self, phone: str) -> PMSPatient | None: ...

    @abstractmethod
    async def check_availability(
        self, date_from: str, date_to: str, preferred_window: str | None = None
    ) -> list[AvailableSlot]: ...

    @abstractmethod
    async def create_appointment(
        self,
        pms_external_id: str,
        appointment_at: str,
        procedure_type: str,
        duration_minutes: int = 60,
    ) -> CreatedAppointment: ...
