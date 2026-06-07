"""Mock Open Dental adapter — deterministic fake data for weekend/demo mode.

Implements the FULL PMSAdapter contract (incl. the reschedule/cancel/provider
methods added in Step 1) so the demo and the contract tests exercise the same
surface the real client will. Keeps a tiny in-memory appointment store so
reschedule/cancel/get behave realistically without a DB.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSOperatory,
    PMSPatient,
    PMSProvider,
)

# A tiny in-memory roster of fake patients.
_FAKE_PATIENTS: dict[str, PMSPatient] = {
    "OD-1001": PMSPatient(
        pms_external_id="OD-1001", first_name="Maria", last_name="Garcia",
        phone="+15551234567", email="maria.garcia@example.com",
        date_of_birth=date(1988, 3, 14),
    ),
    "OD-1002": PMSPatient(
        pms_external_id="OD-1002", first_name="James", last_name="Lee",
        phone="+15557654321", email="james.lee@example.com",
        date_of_birth=date(1975, 11, 2),
    ),
}

_PROVIDERS = [
    PMSProvider(prov_num="1", name="Dr. Smith", abbreviation="SMI"),
    PMSProvider(prov_num="2", name="Dr. Jones", abbreviation="JON"),
]
_OPERATORIES = [
    PMSOperatory(op_num="1", name="Op 1", prov_num="1"),
    PMSOperatory(op_num="2", name="Op 2", prov_num="2"),
]

# Time slots offered, by preferred window.
_WINDOWS = {
    "morning": ["09:00", "10:00", "11:30"],
    "afternoon": ["13:00", "14:30", "16:00"],
    "any": ["09:00", "10:00", "11:30", "13:00", "14:30"],
}


class MockOpenDentalAdapter(PMSAdapter):
    def __init__(self) -> None:
        # In-memory appointment store keyed by AptNum (mock).
        self._appointments: dict[str, CreatedAppointment] = {}
        self._next_apt = 9000

    # ── Patients ─────────────────────────────────────────────────────────────
    async def get_patient(self, pms_external_id: str) -> PMSPatient | None:
        return _FAKE_PATIENTS.get(pms_external_id)

    async def get_patient_by_phone(self, phone: str) -> PMSPatient | None:
        for patient in _FAKE_PATIENTS.values():
            if patient.phone == phone:
                return patient
        return None

    async def create_patient(
        self, first_name: str, last_name: str, phone: str, email: str | None = None
    ) -> PMSPatient:
        new_id = f"OD-{2000 + len(_FAKE_PATIENTS)}"
        patient = PMSPatient(
            pms_external_id=new_id, first_name=first_name, last_name=last_name,
            phone=phone, email=email,
        )
        _FAKE_PATIENTS[new_id] = patient
        return patient

    # ── Reference data ───────────────────────────────────────────────────────
    async def list_providers(self) -> list[PMSProvider]:
        return list(_PROVIDERS)

    async def list_operatories(self) -> list[PMSOperatory]:
        return list(_OPERATORIES)

    # ── Availability + appointments ──────────────────────────────────────────
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
        window = (preferred_window or "any").lower()
        times = _WINDOWS.get(window, _WINDOWS["any"])
        slots: list[AvailableSlot] = []
        for i, t in enumerate(times):
            prov = _PROVIDERS[i % len(_PROVIDERS)]
            op = _OPERATORIES[i % len(_OPERATORIES)]
            slots.append(AvailableSlot(
                date=date_from, time=t, provider=prov.name,
                prov_num=prov_num or prov.prov_num, op_num=op_num or op.op_num,
            ))
        return slots

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
        self._next_apt += 1
        apt_num = str(self._next_apt)
        appt = CreatedAppointment(
            pms_external_id=pms_external_id, apt_num=apt_num,
            appointment_at=appointment_at, duration_minutes=duration_minutes,
            procedure_type=procedure_type, provider_name=_PROVIDERS[0].name,
        )
        self._appointments[apt_num] = appt
        return appt

    async def get_appointment(self, apt_num: str) -> CreatedAppointment | None:
        return self._appointments.get(apt_num)

    async def reschedule_appointment(
        self,
        apt_num: str,
        new_appointment_at: str,
        *,
        op_num: str | None = None,
        prov_num: str | None = None,
    ) -> CreatedAppointment:
        appt = self._appointments.get(apt_num)
        if appt is None:
            # Mirror the real client: an unknown id is an error to the caller.
            raise KeyError(f"appointment {apt_num} not found")
        updated = appt.model_copy(update={"appointment_at": new_appointment_at})
        self._appointments[apt_num] = updated
        return updated

    async def cancel_appointment(
        self, apt_num: str, *, send_to_unscheduled: bool = True
    ) -> bool:
        return self._appointments.pop(apt_num, None) is not None

    # ── Test/seed helper ─────────────────────────────────────────────────────
    @staticmethod
    def next_business_day(from_date: date | None = None) -> str:
        """Helper used by tests / seeding."""
        d = (from_date or datetime.utcnow().date()) + timedelta(days=1)
        while d.weekday() >= 5:  # skip Sat/Sun
            d += timedelta(days=1)
        return d.isoformat()
