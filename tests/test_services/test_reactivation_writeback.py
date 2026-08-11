"""PMS write-back via NexHealth (block 8) — booking client + write_back service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.adapters.nexhealth.client import NexHealthClient
from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.services.reactivation.writeback import write_back_booking
from tests.conftest import seed_practice

_APPT_AT = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
_START = _APPT_AT.isoformat()


def _client(handler) -> NexHealthClient:
    c = NexHealthClient(
        api_key="K", subdomain="sub", location_id="42",
        base_url="https://nh.test", transport=httpx.MockTransport(handler),
    )
    c._retry_base_delay = 0
    return c


def _slot_handler(*, slot_free=True, create_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        if path == "/appointment_slots":
            rows = (
                [{"pid": "p1", "slots": [{"time": _START, "operatory_id": "op1"}]}]
                if slot_free else
                [{"pid": "p1", "slots": []}]
            )
            return httpx.Response(200, json={"data": rows})
        if path == "/appointments":
            if not create_ok:
                return httpx.Response(500)
            appt = {"id": "NH-APPT-9", "start_time": _START}
            return httpx.Response(201, json={"data": {"appt": appt}})
        return httpx.Response(404)
    return handler


# ── client booking methods ────────────────────────────────────────────────
async def test_find_slots_and_create_appointment():
    c = _client(_slot_handler())
    slots = await c.find_appointment_slots(start_date="2026-07-01", days=1, provider_ids=["p1"])
    assert len(slots) == 1
    assert slots[0].provider_id == "p1" and slots[0].operatory_id == "op1"

    appt = await c.create_appointment(
        patient_pms_id="nh-1", provider_id="p1", start_time=_START, operatory_id="op1"
    )
    assert appt.appointment_id == "NH-APPT-9"


# ── write-back service (DB) ───────────────────────────────────────────────
async def _seed_booking(db_session, practice):
    await set_tenant(db_session, practice.id)
    pat = Patient(id=uuid.uuid4(), practice_id=practice.id, pms_external_id="nh-1",
                  first_name="Maria", phone="+15551110001")
    db_session.add(pat)
    await db_session.flush()
    bk = Booking(id=uuid.uuid4(), practice_id=practice.id, patient_id=pat.id,
                 appointment_at=_APPT_AT, duration_minutes=60,
                 status="confirmed", source="reactivation")
    db_session.add(bk)
    await db_session.commit()
    return bk


async def test_write_back_written_sets_pms_id(db_session):
    practice, _ = await seed_practice(
        db_session, name="WB1", clerk_org_id="org_wb1", clerk_user_id="u_wb1"
    )
    bk = await _seed_booking(db_session, practice)
    status = await write_back_booking(
        db_session, practice.id, bk, patient_pms_id="nh-1", provider_id="p1",
        operatory_id="op1", client=_client(_slot_handler(slot_free=True)),
    )
    assert status == "written"
    await set_tenant(db_session, practice.id)
    refreshed = (await db_session.execute(
        select(Booking).where(Booking.id == bk.id)
    )).scalar_one()
    assert refreshed.pms_external_id == "NH-APPT-9"


async def test_write_back_conflict_when_slot_taken(db_session):
    practice, _ = await seed_practice(
        db_session, name="WB2", clerk_org_id="org_wb2", clerk_user_id="u_wb2"
    )
    bk = await _seed_booking(db_session, practice)
    status = await write_back_booking(
        db_session, practice.id, bk, patient_pms_id="nh-1", provider_id="p1",
        client=_client(_slot_handler(slot_free=False)),
    )
    assert status == "conflict"
    await set_tenant(db_session, practice.id)
    refreshed = (await db_session.execute(
        select(Booking).where(Booking.id == bk.id)
    )).scalar_one()
    assert refreshed.pms_external_id is None  # nothing written


async def test_write_back_graceful_on_pms_unavailable(db_session):
    practice, _ = await seed_practice(
        db_session, name="WB3", clerk_org_id="org_wb3", clerk_user_id="u_wb3"
    )
    bk = await _seed_booking(db_session, practice)

    # /appointment_slots 500s → NexHealthUnavailable → graceful, no crash.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(200, json={"data": {"token": "T"}})
        return httpx.Response(503)

    status = await write_back_booking(
        db_session, practice.id, bk, patient_pms_id="nh-1", provider_id="p1",
        client=_client(handler),
    )
    assert status == "pms_unavailable"
    await set_tenant(db_session, practice.id)
    refreshed = (await db_session.execute(
        select(Booking).where(Booking.id == bk.id)
    )).scalar_one()
    assert refreshed.pms_external_id is None  # left un-synced for retry


# ---------------------------------------------------------------------------
# The bridge decides which PMS answers, and this file used to ignore it.
#
# It constructed a NexHealthClient unconditionally while app.adapters.bridge
# preferred Kolla. On our first customer's practice — Eaglesoft, reachable only
# through Kolla — every voice booking would have been created in our database,
# confirmed to the patient by SMS, and never written to the calendar the front
# desk reads. The only trace was an alert in an in-process ring buffer.
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Answers like whichever bridge you tell it to be, and remembers the call."""

    def __init__(self, slots):
        self._slots = slots
        self.created: dict | None = None

    async def find_appointment_slots(self, **kwargs):
        return self._slots

    async def create_appointment(self, **kwargs):
        self.created = kwargs
        from app.adapters.nexhealth.models import PmsAppointment

        return PmsAppointment(appointment_id="PMS-1", start_time=kwargs["start_time"])


def _slot(start, provider="", operatory=None):
    from app.adapters.nexhealth.models import PmsSlot

    return PmsSlot(start_time=start, provider_id=provider, operatory_id=operatory)


async def test_a_kolla_slot_is_not_read_as_taken(db_session):
    """Kolla schedules rooms and leaves the provider blank. Requiring a provider
    match made every one of its slots look busy, so the write never happened and
    the status said 'conflict' — the one word that makes a human think the clinic
    took the time, rather than that we never asked."""
    from app.services.reactivation.writeback import write_back_booking

    practice, booking = await _clinic_with_a_booking(db_session)
    start = booking.appointment_at.isoformat()
    client = _RecordingClient([_slot(start, provider="", operatory="resources/op-1")])

    status = await write_back_booking(
        db_session, practice.id, booking,
        patient_pms_id="contacts/9", provider_id="",
        operatory_id="resources/op-1", client=client,
    )
    assert status == "written"
    assert booking.pms_external_id == "PMS-1"


async def test_a_different_room_at_the_same_time_is_still_taken(db_session):
    """The time matching is not enough on its own when both sides name a room."""
    from app.services.reactivation.writeback import write_back_booking

    practice, booking = await _clinic_with_a_booking(db_session)
    start = booking.appointment_at.isoformat()
    client = _RecordingClient([_slot(start, operatory="resources/op-2")])

    status = await write_back_booking(
        db_session, practice.id, booking,
        patient_pms_id="contacts/9", provider_id="",
        operatory_id="resources/op-1", client=client,
    )
    assert status == "conflict"
    assert client.created is None


async def test_the_write_carries_an_end_time_because_kolla_requires_one(db_session):
    """NexHealth derives the end from the appointment type; Kolla refuses without
    it. Both clients take the same call so this path never has to know which
    bridge it is holding."""
    from app.services.reactivation.writeback import write_back_booking

    practice, booking = await _clinic_with_a_booking(db_session)
    start = booking.appointment_at.isoformat()
    client = _RecordingClient([_slot(start, provider="prov-1")])

    await write_back_booking(
        db_session, practice.id, booking,
        patient_pms_id="1", provider_id="prov-1", client=client,
    )
    assert client.created["end_time"] > client.created["start_time"]


async def test_a_clinic_with_no_bridge_is_not_an_error(db_session, monkeypatch):
    """A practice with no PMS connected has its whole calendar in our book.
    Reporting that as a failed write would alert on every booking it ever takes
    and bury the statuses that mean something.

    Writes are turned ON here deliberately: otherwise this asserts the safety
    switch rather than the case it was written for, and would keep passing after
    the behaviour it describes had gone.
    """
    from app.services.reactivation import writeback

    monkeypatch.setattr(
        writeback, "get_settings",
        lambda: type("S", (), {"pms_write_enabled": True})(),
    )
    practice, booking = await _clinic_with_a_booking(db_session)
    status = await writeback.write_back_booking(
        db_session, practice.id, booking, patient_pms_id="1", provider_id="p",
    )
    assert status == "no_pms"
    assert booking.pms_external_id is None


async def _clinic_with_a_booking(db_session):
    """A practice and one confirmed booking, named so each test gets its own."""
    import uuid as _uuid

    suffix = _uuid.uuid4().hex[:6]
    practice, _ = await seed_practice(
        db_session, name=f"Bridge {suffix}",
        clerk_org_id=f"org_{suffix}", clerk_user_id=f"u_{suffix}",
    )
    return practice, await _seed_booking(db_session, practice)


# ---------------------------------------------------------------------------
# Reading a clinic's calendar and writing to it are separate risks, so they are
# separate decisions. A new practice connects read-only: the agent offers its
# real openings straight away, which is most of the value, while every write
# stays in our book until somebody has compared the two calendars by eye.
# ---------------------------------------------------------------------------


async def test_writes_are_off_until_somebody_turns_them_on(db_session, monkeypatch):
    """The default. A wrong write is a patient shown a time the clinic cannot
    honour, or a chair held empty — and the front desk finds out before we do."""
    from app.services.reactivation import writeback

    practice, booking = await _clinic_with_a_booking(db_session)
    monkeypatch.setattr(
        writeback, "get_settings",
        lambda: type("S", (), {"pms_write_enabled": False})(),
    )
    status = await writeback.write_back_booking(
        db_session, practice.id, booking, patient_pms_id="1", provider_id="p",
    )
    assert status == "write_disabled"
    assert booking.pms_external_id is None


async def test_cancel_and_move_are_off_by_the_same_switch(db_session, monkeypatch):
    """One switch, or somebody turns on two thirds of it and finds out which
    third they missed from a clinic."""
    from app.services.reactivation import writeback

    practice, booking = await _clinic_with_a_booking(db_session)
    booking.pms_external_id = "appointments/1"
    await db_session.commit()
    monkeypatch.setattr(
        writeback, "get_settings",
        lambda: type("S", (), {"pms_write_enabled": False})(),
    )
    assert await writeback.cancel_in_pms(db_session, practice.id, booking) == "write_disabled"
    assert await writeback.move_in_pms(db_session, practice.id, booking) == "write_disabled"


async def test_an_explicit_client_still_writes(db_session, monkeypatch):
    """The switch governs production wiring, not the tests and not a deliberate
    call with a client in hand — otherwise every write test would be testing the
    switch instead of the write."""
    from app.services.reactivation import writeback

    practice, booking = await _clinic_with_a_booking(db_session)
    start = booking.appointment_at.isoformat()
    client = _RecordingClient([_slot(start, provider="prov-1")])
    monkeypatch.setattr(
        writeback, "get_settings",
        lambda: type("S", (), {"pms_write_enabled": False})(),
    )
    status = await writeback.write_back_booking(
        db_session, practice.id, booking,
        patient_pms_id="1", provider_id="prov-1", client=client,
    )
    assert status == "written"
