"""An appointment that exists in our book and in no practice's software.

The write-back can be refused, the patient can be unknown to their system, the
slot can carry no provider. Each of those left a booking the practice never saw,
an alert in our logs, and a line on the booking page — "Not yet, this
appointment is only in Dentovox" — with nothing anybody could press. A live
clinic told us their calendar was missing appointments before we noticed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.services import push_to_practice
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": user and org}


class _Slot:
    def __init__(self, date, time, prov_num="7", op_num="2"):
        self.date, self.time = date, time
        self.prov_num, self.op_num = prov_num, op_num
        self.provider = "Dr. Zimlensky"


async def _seed(db_session, tag: str, *, patient_pms_id: str):
    practice, _ = await seed_practice(
        db_session, name=f"Push {tag}",
        clerk_org_id=f"org_p{tag}", clerk_user_id=f"user_p{tag}",
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=patient_pms_id,
        first_name="Ruth", last_name="Delaney", phone="+15551239000",
    )
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=3),
        duration_minutes=45, procedure_type="cleaning", status="confirmed",
    )
    db_session.add(booking)
    await db_session.commit()
    return practice, booking


async def test_the_front_desk_can_push_it_across(client, db_session, monkeypatch):
    practice, booking = await _seed(db_session, "a", patient_pms_id="88123")
    written: dict = {}

    async def _slots(prac, **kw):
        from app.services.availability import slot_from_utc
        d, t = slot_from_utc(booking.appointment_at, prac.timezone)
        return [_Slot(d, t)]

    async def _write(session, practice_id, b, **kw):
        written.update(kw)
        b.pms_external_id = "1677199999"
        return "written"

    monkeypatch.setattr(push_to_practice, "compute_pms_slots", _slots)
    monkeypatch.setattr(push_to_practice, "write_back_booking", _write)

    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_pa", "user_pa")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["in_pms"] is True
    assert body["pms_sync_status"] is None
    # The provider and chair come from the practice's own calendar, not from us.
    assert written["provider_id"] == "7"
    assert written["operatory_id"] == "2"


async def test_a_patient_their_system_has_never_seen_says_so(
    client, db_session, monkeypatch
):
    """A VOICE- id is one we invented for a caller. There is nothing in their
    software to attach an appointment to, and "sync failed" does not say that."""
    _, booking = await _seed(db_session, "b", patient_pms_id="VOICE-12AB34CD")

    async def _slots(*_a, **_kw):
        raise AssertionError("their calendar should not even be asked")

    monkeypatch.setattr(push_to_practice, "compute_pms_slots", _slots)

    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_pb", "user_pb")
    )
    assert r.status_code == 200, r.text
    assert "Add them there" in r.json()["pms_sync_status"]


async def test_a_time_their_calendar_no_longer_has_open_says_so(
    client, db_session, monkeypatch
):
    _, booking = await _seed(db_session, "c", patient_pms_id="88123")
    monkeypatch.setattr(
        push_to_practice, "compute_pms_slots",
        lambda *_a, **_kw: _empty(),
    )

    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_pc", "user_pc")
    )
    assert r.status_code == 200, r.text
    assert "Move it to a free time" in r.json()["pms_sync_status"]
    assert r.json()["in_pms"] is False


async def _empty():
    return []


@pytest.mark.asyncio
async def test_a_cancelled_booking_that_never_reached_them_is_not_pushed(
    client, db_session
):
    """Creating an appointment nobody is coming to is worse than the gap."""
    _, booking = await _seed(db_session, "d", patient_pms_id="88123")
    await set_tenant(db_session, booking.practice_id)
    booking.status = "cancelled"
    await db_session.commit()

    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_pd", "user_pd")
    )
    assert r.status_code == 409
