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
