import pytest

from app.adapters.open_dental.mock import MockOpenDentalAdapter


@pytest.fixture
def adapter():
    return MockOpenDentalAdapter()


async def test_get_patient_known(adapter):
    p = await adapter.get_patient("OD-1001")
    assert p is not None
    assert p.first_name == "Maria"


async def test_get_patient_unknown(adapter):
    assert await adapter.get_patient("nope") is None


async def test_get_patient_by_phone(adapter):
    p = await adapter.get_patient_by_phone("+15551234567")
    assert p is not None and p.last_name == "Garcia"


async def test_check_availability_morning(adapter):
    slots = await adapter.check_availability("2026-06-05", "2026-06-05", "morning")
    assert len(slots) == 3
    assert all(s.date == "2026-06-05" for s in slots)
    assert slots[0].time == "09:00"


async def test_create_appointment(adapter):
    appt = await adapter.create_appointment(
        "OD-1001", "2026-06-05T10:00:00Z", "cleaning", 60
    )
    assert appt.pms_external_id.startswith("APPT-")
    assert appt.duration_minutes == 60
