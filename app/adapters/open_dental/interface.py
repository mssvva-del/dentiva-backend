"""Abstract PMS adapter interface. Mock + real implementations conform to this.

The voice flow (availability → book → reschedule/cancel) talks ONLY to this
contract, so swapping mock ↔ real Open Dental is a config flip per practice.
Reschedule/cancel/provider methods were added in the Open Dental Step 1 work —
the original interface only had availability + create, which is why reschedule
and cancel currently live in our DB only (they need these methods to reach the
real PMS).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSOperatory,
    PMSPatient,
    PMSProvider,
)


class PMSAdapter(ABC):
    # ── Patients ─────────────────────────────────────────────────────────────
    @abstractmethod
    async def get_patient(self, pms_external_id: str) -> PMSPatient | None: ...

    @abstractmethod
    async def get_patient_by_phone(self, phone: str) -> PMSPatient | None: ...

    @abstractmethod
    async def create_patient(
        self, first_name: str, last_name: str, phone: str, email: str | None = None
    ) -> PMSPatient:
        """Create a new patient (for first-time callers with no PMS record)."""

    # ── Scheduling reference data ────────────────────────────────────────────
    @abstractmethod
    async def list_providers(self) -> list[PMSProvider]:
        """Dentists/hygienists. Needed to resolve a ProvNum for slots/booking."""

    @abstractmethod
    async def list_operatories(self) -> list[PMSOperatory]:
        """Chairs/rooms. Needed to resolve an OpNum for slots/booking."""

    # ── Availability + appointments ──────────────────────────────────────────
    @abstractmethod
    async def check_availability(
        self,
        date_from: str,
        date_to: str,
        preferred_window: str | None = None,
        *,
        length_minutes: int = 60,
        prov_num: str | None = None,
        op_num: str | None = None,
    ) -> list[AvailableSlot]:
        """Open slots in [date_from, date_to]. prov_num/op_num narrow the search;
        the real PMS requires at least one to return real openings."""

    @abstractmethod
    async def create_appointment(
        self,
        pms_external_id: str,
        appointment_at: str,
        procedure_type: str,
        duration_minutes: int = 60,
        *,
        op_num: str | None = None,
        prov_num: str | None = None,
    ) -> CreatedAppointment:
        """Book an appointment. Returns the created appointment incl. its AptNum."""

    @abstractmethod
    async def get_appointment(self, apt_num: str) -> CreatedAppointment | None:
        """Fetch one appointment by its PMS AptNum (for sync/verification)."""

    @abstractmethod
    async def reschedule_appointment(
        self,
        apt_num: str,
        new_appointment_at: str,
        *,
        op_num: str | None = None,
        prov_num: str | None = None,
    ) -> CreatedAppointment:
        """Move an existing appointment to a new time/operatory."""

    @abstractmethod
    async def cancel_appointment(
        self, apt_num: str, *, send_to_unscheduled: bool = True
    ) -> bool:
        """Cancel/break an appointment. Returns True on success."""
