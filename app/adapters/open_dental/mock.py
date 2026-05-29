"""Mock Open Dental adapter — deterministic fake data for weekend mode."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSPatient,
)

# A tiny in-memory roster of fake patients.
_FAKE_PATIENTS: dict[str, PMSPatient] = {
    "OD-1001": PMSPatient(
        pms_external_id="OD-1001",
        first_name="Maria",
        last_name="Garcia",
        phone="+15551234567",
        email="maria.garcia@example.com",
        date_of_birth=date(1988, 3, 14),
    ),
    "OD-1002": PMSPatient(
        pms_external_id="OD-1002",
        first_name="James",
        last_name="Lee",
        phone="+15557654321",
        email="james.lee@example.com",
        date_of_birth=date(1975, 11, 2),
    ),
}

_PROVIDERS = ["Dr. Smith", "Dr. Jones"]
# Time slots offered, by preferred window.
_WINDOWS = {
    "morning": ["09:00", "10:00", "11:30"],
    "afternoon": ["13:00", "14:30", "16:00"],
    "any": ["09:00", "10:00", "11:30", "13:00", "14:30"],
}


class MockOpenDentalAdapter(PMSAdapter):
    async def get_patient(self, pms_external_id: str) -> PMSPatient | None:
        return _FAKE_PATIENTS.get(pms_external_id)

    async def get_patient_by_phone(self, phone: str) -> PMSPatient | None:
        for patient in _FAKE_PATIENTS.values():
            if patient.phone == phone:
                return patient
        return None

    async def check_availability(
        self, date_from: str, date_to: str, preferred_window: str | None = None
    ) -> list[AvailableSlot]:
        window = (preferred_window or "any").lower()
        times = _WINDOWS.get(window, _WINDOWS["any"])
        slots: list[AvailableSlot] = []
        for i, t in enumerate(times):
            slots.append(
                AvailableSlot(date=date_from, time=t, provider=_PROVIDERS[i % len(_PROVIDERS)])
            )
        return slots

    async def create_appointment(
        self,
        pms_external_id: str,
        appointment_at: str,
        procedure_type: str,
        duration_minutes: int = 60,
    ) -> CreatedAppointment:
        # Generate a deterministic fake external appointment id.
        suffix = abs(hash((pms_external_id, appointment_at))) % 100000
        return CreatedAppointment(
            pms_external_id=f"APPT-{suffix:05d}",
            appointment_at=appointment_at,
            duration_minutes=duration_minutes,
            procedure_type=procedure_type,
            provider_name=_PROVIDERS[0],
        )

    @staticmethod
    def next_business_day(from_date: date | None = None) -> str:
        """Helper used by tests / seeding."""
        d = (from_date or datetime.utcnow().date()) + timedelta(days=1)
        while d.weekday() >= 5:  # skip Sat/Sun
            d += timedelta(days=1)
        return d.isoformat()
