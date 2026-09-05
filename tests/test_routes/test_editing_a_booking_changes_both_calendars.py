"""An appointment lives in two calendars. Editing one has to edit the other.

Until now the dashboard could only mark a booking cancelled or no-show — and
even the cancellation stopped at our database. cancel_in_pms had been written,
commented, and never called, so a chair the patient gave up stayed blocked
against everyone else in the practice's own software.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.booking import Booking
from app.models.patient import Patient
from app.services import booking_edits
from tests.conftest import seed_practice


def _headers(tag: str) -> dict[str, str]:
    return {"X-Dev-Clerk-User-Id": f"u_edit_{tag}",
            "X-Dev-Clerk-Org-Id": f"org_edit_{tag}"}


async def _setup(db, tag: str, *, at: datetime, minutes: int = 45):
    practice, _ = await seed_practice(
        db, name=f"Edit {tag}", clerk_org_id=f"org_edit_{tag}",
        clerk_user_id=f"u_edit_{tag}",
    )
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, first_name="Ada",
        last_name="Lovelace", phone="+16175550188",
        pms_external_id=f"P-{uuid.uuid4().hex[:6]}",
    )
    db.add(patient)
    await db.commit()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=at, duration_minutes=minutes, procedure_type="cleaning",
        status="confirmed", source="ai_call",
        pms_external_id=f"NH-{uuid.uuid4().hex[:6]}",
    )
    db.add(booking)
    await db.commit()
    return practice, booking


async def test_moving_an_appointment_moves_it_in_the_clinics_calendar(
    client, db_session, monkeypatch
):
    moved: list = []

    async def _spy(_session, practice_id, booking, **_kw):
        moved.append((practice_id, booking.appointment_at))
        return "moved"

    monkeypatch.setattr(booking_edits, "move_in_pms", _spy)

    at = datetime.now(UTC) + timedelta(days=3)
    practice, booking = await _setup(db_session, "a", at=at)
    new_time = (at + timedelta(hours=2)).replace(microsecond=0)

    r = await client.patch(
        f"/api/bookings/{booking.id}",
        headers=_headers("a"),
        json={"appointment_at": new_time.isoformat()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["appointment_at"].startswith(new_time.isoformat()[:16])
    assert moved, "we moved it here and never told the practice's calendar"
    assert moved[0][1].replace(microsecond=0) == new_time


async def test_cancelling_frees_the_chair_in_the_clinics_calendar(
    client, db_session, monkeypatch
):
    cancelled: list = []

    async def _spy(_session, practice_id, booking, **_kw):
        cancelled.append(booking.id)
        return "cancelled"

    monkeypatch.setattr(booking_edits, "cancel_in_pms", _spy)

    at = datetime.now(UTC) + timedelta(days=4)
    _practice, booking = await _setup(db_session, "b", at=at)

    r = await client.patch(
        f"/api/bookings/{booking.id}/status",
        headers=_headers("b"),
        json={"status": "cancelled"},
    )
    assert r.status_code == 200, r.text
    assert cancelled == [booking.id], (
        "the patient is gone and the practice still has the hour blocked"
    )


async def test_an_edit_cannot_park_two_patients_in_one_chair(
    client, db_session, monkeypatch
):
    """The unique constraint catches an exact collision and cannot see an
    overlap: a 60-minute prep moved onto the half hour before a cleaning slides
    through it. An editor that lets a human do deliberately what the agent is
    stopped from doing by accident is not a guard."""
    monkeypatch.setattr(
        booking_edits, "move_in_pms",
        lambda *a, **k: _noop(),
    )

    at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=5, hours=1)
    practice, first = await _setup(db_session, "c", at=at, minutes=60)
    second = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=first.patient_id,
        appointment_at=at + timedelta(hours=2), duration_minutes=45,
        procedure_type="cleaning", status="confirmed", source="ai_call",
    )
    db_session.add(second)
    await db_session.commit()

    # Move the second one back so it overlaps the first by fifteen minutes.
    r = await client.patch(
        f"/api/bookings/{second.id}",
        headers=_headers("c"),
        json={"appointment_at": (at + timedelta(minutes=45)).isoformat()},
    )
    assert r.status_code == 409, r.text
    assert "already covers that time" in r.text


async def _noop():
    return "moved"


async def test_an_edit_names_only_what_it_changes(client, db_session, monkeypatch):
    """A reschedule must not blank the procedure somebody typed last week."""
    monkeypatch.setattr(booking_edits, "move_in_pms", lambda *a, **k: _noop())

    at = datetime.now(UTC) + timedelta(days=6)
    _practice, booking = await _setup(db_session, "d", at=at)

    r = await client.patch(
        f"/api/bookings/{booking.id}",
        headers=_headers("d"),
        json={"duration_minutes": 90},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duration_minutes"] == 90
    assert body["procedure_type"] == "cleaning", "an untouched field was wiped"


async def test_a_cancelled_appointment_is_not_edited_back_to_life(
    client, db_session
):
    at = datetime.now(UTC) + timedelta(days=7)
    _practice, booking = await _setup(db_session, "e", at=at)
    booking.status = "cancelled"
    await db_session.commit()

    r = await client.patch(
        f"/api/bookings/{booking.id}",
        headers=_headers("e"),
        json={"appointment_at": (at + timedelta(hours=1)).isoformat()},
    )
    assert r.status_code == 409
    assert "Book a new one" in r.text


async def test_an_empty_edit_is_refused(client, db_session):
    at = datetime.now(UTC) + timedelta(days=8)
    _practice, booking = await _setup(db_session, "f", at=at)
    r = await client.patch(
        f"/api/bookings/{booking.id}", headers=_headers("f"), json={},
    )
    assert r.status_code == 422


async def test_the_edit_is_written_to_the_audit_log(client, db_session, monkeypatch):
    monkeypatch.setattr(booking_edits, "move_in_pms", lambda *a, **k: _noop())
    from app.models.audit_log import AuditLog

    at = datetime.now(UTC) + timedelta(days=9)
    practice, booking = await _setup(db_session, "g", at=at)
    await client.patch(
        f"/api/bookings/{booking.id}",
        headers=_headers("g"),
        json={"procedure_type": "crown prep"},
    )
    rows = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.practice_id == practice.id,
            AuditLog.action == "booking_edited",
        )
    )).scalars().all()
    assert rows, "a person changed a patient's appointment and nothing recorded it"
    assert rows[0].audit_metadata["before"]["procedure_type"] == "cleaning"


async def test_a_booking_months_out_is_still_on_the_list(client, db_session):
    """The list stopped thirty days out by default. An appointment the agent
    booked for November simply was not on the screen in September — no empty
    state, no note about a range, just a shorter list. The clinic reasonably
    concluded the booking had been lost.
    """
    at = datetime.now(UTC) + timedelta(days=61)
    _practice, booking = await _setup(db_session, "h", at=at)

    r = await client.get("/api/bookings", headers=_headers("h"))
    assert r.status_code == 200, r.text
    ids = [b["id"] for b in r.json()["bookings"]]
    assert str(booking.id) in ids, "a real appointment was hidden by a default"


async def test_a_booking_says_whether_the_practice_calendar_took_it(
    client, db_session
):
    """A booking their software refused looks identical to one it accepted —
    same badge, same row — and the patient has already been told they are
    booked. It happened twice on this clinic in one afternoon."""
    at = datetime.now(UTC) + timedelta(days=11)
    practice, synced = await _setup(db_session, "i", at=at)

    ours_only = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=synced.patient_id,
        appointment_at=at + timedelta(days=1), duration_minutes=45,
        procedure_type="cleaning", status="confirmed", source="ai_call",
        pms_external_id=None,          # the PMS refused it
    )
    db_session.add(ours_only)
    await db_session.commit()

    r = await client.get("/api/bookings", headers=_headers("i"))
    rows = {b["id"]: b["in_pms"] for b in r.json()["bookings"]}
    assert rows[str(synced.id)] is True
    assert rows[str(ours_only.id)] is False, (
        "an appointment the clinic's calendar never took looks accepted"
    )


async def test_a_slot_taken_while_the_caller_chose_is_not_confirmed(
    client, db_session, monkeypatch
):
    """The times offered a minute ago were free a minute ago.

    On a live pair of calls one patient took nine o'clock while the next was
    still choosing, and the agent confirmed quarter past — inside the
    forty-five minutes the first patient now owned. The practice's software
    refused the write, correctly, and the caller had already been told they
    were booked.
    """
    from app.webhooks import retell as retell_mod

    practice, _ = await _setup(
        db_session, "j", at=datetime.now(UTC) + timedelta(days=20)
    )
    # Connected to a PMS that no longer has the slot the agent picked.
    monkeypatch.setattr(retell_mod, "pms_is_connected", lambda _p: True)

    from app.adapters.open_dental.models import AvailableSlot

    offered = AvailableSlot(date="2099-10-05", time="09:15", provider="our team")
    calls: list[int] = []

    async def _free_then_taken(*_a, **_kw):
        # Free when the caller was offered it; gone by the time they said yes.
        calls.append(1)
        return [offered] if len(calls) == 1 else []

    monkeypatch.setattr(retell_mod, "compute_pms_slots", _free_then_taken)

    r = await client.post("/webhooks/retell", json={
        "name": "book_appointment",
        "call": {"call_id": f"taken-{uuid.uuid4().hex[:8]}",
                 "metadata": {"practice_id": str(practice.id)}},
        "args": {"patient_first_name": "Nadia", "patient_phone": "+16175550123",
                 "preferred_date": "2099-10-05", "preferred_time": "09:15",
                 "procedure": "cleaning"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("booked") is False, (
        "the caller was told yes for a time the practice no longer had"
    )
    assert "just taken" in body["message"]


async def test_a_pms_we_cannot_reach_does_not_block_a_live_booking(
    client, db_session, monkeypatch
):
    """A slow or broken PMS must not cost the caller their appointment. Falling
    back to our own book is what every other path here does, and the write-back
    still refuses a genuine collision afterwards."""
    from app.webhooks import retell as retell_mod

    practice, _ = await _setup(
        db_session, "k", at=datetime.now(UTC) + timedelta(days=21)
    )
    monkeypatch.setattr(retell_mod, "pms_is_connected", lambda _p: True)

    from app.adapters.open_dental.models import AvailableSlot

    offered = AvailableSlot(date="2099-10-06", time="09:15", provider="our team")
    calls: list[int] = []

    async def _then_down(*_a, **_kw):
        # Answers while the caller is choosing, times out when they say yes.
        calls.append(1)
        if len(calls) == 1:
            return [offered]
        raise TimeoutError("PMS unreachable")

    monkeypatch.setattr(retell_mod, "compute_pms_slots", _then_down)

    r = await client.post("/webhooks/retell", json={
        "name": "book_appointment",
        "call": {"call_id": f"down-{uuid.uuid4().hex[:8]}",
                 "metadata": {"practice_id": str(practice.id)}},
        "args": {"patient_first_name": "Nadia", "patient_phone": "+16175550124",
                 "preferred_date": "2099-10-06", "preferred_time": "09:15",
                 "procedure": "cleaning"},
    })
    assert r.status_code == 200, r.text
    assert r.json().get("booked") is True, "a PMS outage cost the caller a booking"


async def test_a_slot_we_just_filled_is_not_offered_again(
    client, db_session, monkeypatch
):
    """The practice's calendar lags on what we wrote to it a minute ago. Its
    availability endpoint still listed 9:15 while our own book held 9:00–9:45,
    so the next caller was offered it, told "you're booked", and the write-back
    was refused as a conflict. Both bookings were ours; one ever existed there."""
    from app.adapters.open_dental.models import AvailableSlot
    from app.webhooks import retell as retell_mod

    practice, _ = await _setup(
        db_session, "z", at=datetime(2099, 10, 5, 13, 0, tzinfo=UTC)  # 09:00 EDT, 45 min
    )
    monkeypatch.setattr(retell_mod, "pms_is_connected", lambda _p: True)

    async def _pms_says(*_a, **_kw):
        return [AvailableSlot(date="2099-10-05", time="09:15", provider="our team"),
                AvailableSlot(date="2099-10-05", time="10:30", provider="our team")]

    monkeypatch.setattr(retell_mod, "compute_pms_slots", _pms_says)

    r = await client.post("/webhooks/retell", json={
        "name": "check_availability",
        "call": {"call_id": f"lag-{uuid.uuid4().hex[:8]}",
                 "metadata": {"practice_id": str(practice.id)}},
        "args": {"preferred_date": "2099-10-05", "procedure": "cleaning"},
    })
    times = [s["time"] for s in r.json().get("available_slots", [])]
    assert "09:15" not in times, times   # inside our own 09:00–09:45
    assert "10:30" in times


async def test_the_pre_commit_check_trusts_our_own_book_over_a_lagging_pms(
    client, db_session, monkeypatch
):
    """Even when the practice's calendar still says the slot is open."""
    from app.adapters.open_dental.models import AvailableSlot
    from app.webhooks import retell as retell_mod

    practice, _ = await _setup(
        db_session, "y", at=datetime(2099, 10, 6, 13, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(retell_mod, "pms_is_connected", lambda _p: True)
    offered = AvailableSlot(date="2099-10-06", time="09:15", provider="our team")

    async def _still_open(*_a, **_kw):
        return [offered]   # the PMS view, lagging

    monkeypatch.setattr(retell_mod, "compute_pms_slots", _still_open)
    # Reach the pre-commit check directly with the lagging slot: availability
    # would already have hidden it, but a caller can name a time unprompted.
    monkeypatch.setattr(retell_mod, "_minus_our_own_book",
                        retell_mod._minus_our_own_book)  # real one

    r = await client.post("/webhooks/retell", json={
        "name": "book_appointment",
        "call": {"call_id": f"lag2-{uuid.uuid4().hex[:8]}",
                 "metadata": {"practice_id": str(practice.id)}},
        "args": {"patient_first_name": "Nora", "patient_phone": "+16175550125",
                 "preferred_date": "2099-10-06", "preferred_time": "09:15",
                 "procedure": "cleaning"},
    })
    assert r.status_code == 200, r.text
    assert r.json().get("booked") is not True, r.json()
