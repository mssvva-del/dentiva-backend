"""A cancellation the practice's software refuses.

Three of them sat in a live clinic's calendar this morning. Our screens said
Cancelled, the chairs stayed blocked, and the only trace was one line in a log:
"pms_error". Nothing on any screen said the two calendars disagreed, and nothing
could ask the practice's system a second time — the appointment was already
cancelled here, so no code path would ever call them again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.nexhealth.client import NexHealthError
from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from app.services.booking_edits import apply_cancellation
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


class _RefusingPms:
    """The clinic's system, answering the way theirs did."""

    def __init__(self, message="NexHealth 400 on PATCH /appointments/1 — Cannot update"):
        self.message = message
        self.calls = 0

    async def cancel_appointment(self, _appointment_id, **_kw):
        self.calls += 1
        raise NexHealthError(self.message)


class _AcceptingPms:
    def __init__(self):
        self.calls = 0

    async def cancel_appointment(self, _appointment_id, **_kw):
        self.calls += 1


async def _seed(db_session, tag: str):
    practice, _ = await seed_practice(
        db_session, name=f"Sync {tag}",
        clerk_org_id=f"org_s{tag}", clerk_user_id=f"user_s{tag}",
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=f"s-{tag}",
        first_name="Ruth", last_name="Delaney", phone="+15551239000",
    )
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=2),
        duration_minutes=45, procedure_type="cleaning", status="cancelled",
        pms_external_id="1677173079",
    )
    db_session.add(booking)
    await db_session.commit()
    return practice, booking


@pytest.mark.asyncio
async def test_a_refusal_is_kept_in_the_practices_own_words(db_session, monkeypatch):
    from app.services.reactivation import writeback

    practice, booking = await _seed(db_session, "b")
    pms = _RefusingPms("NexHealth 400 on PATCH /appointments/1 — "
                       "Cannot update already cancelled appointment")
    monkeypatch.setattr(writeback, "_bridge_for", lambda *_a, **_kw: _wrap(pms))
    monkeypatch.setattr(writeback.get_settings(), "pms_write_enabled", True, raising=False)

    outcome = await apply_cancellation(db_session, practice.id, booking)

    assert outcome == "pms_error"
    # Not "pms_error" — the sentence their software actually answered with, so
    # the front desk knows whether to try again or ring the practice.
    assert "already cancelled" in (booking.pms_sync_status or ""), booking.pms_sync_status


async def _wrap(client):
    return client


async def test_the_front_desk_can_ask_their_system_again(client, db_session, monkeypatch):
    """Nothing would call the PMS a second time: the appointment was already
    cancelled here, so every path that syncs had already run."""
    from app.services.reactivation import writeback

    practice, booking = await _seed(db_session, "c")
    booking.pms_sync_status = "Your practice software refused it"
    await db_session.commit()

    pms = _AcceptingPms()
    monkeypatch.setattr(writeback, "_bridge_for", lambda *_a, **_kw: _wrap(pms))
    monkeypatch.setattr(writeback.get_settings(), "pms_write_enabled", True, raising=False)

    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_sc", "user_sc")
    )
    assert r.status_code == 200, r.text
    assert pms.calls == 1, "the practice's system was never asked again"
    assert r.json()["pms_sync_status"] is None, "still shows as out of step"


async def test_an_appointment_that_never_reached_them_cannot_be_resynced(
    client, db_session
):
    practice, booking = await _seed(db_session, "d")
    booking.pms_external_id = None
    await db_session.commit()

    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_sd", "user_sd")
    )
    assert r.status_code == 409


async def test_one_practice_cannot_resync_anothers_booking(client, db_session):
    _, booking = await _seed(db_session, "e")
    await seed_practice(
        db_session, name="Someone Else", clerk_org_id="org_sx", clerk_user_id="user_sx"
    )
    await db_session.commit()
    r = await client.post(
        f"/api/bookings/{booking.id}/resync", headers=_auth("org_sx", "user_sx")
    )
    assert r.status_code == 404
