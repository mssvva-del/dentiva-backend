"""Tests for the reschedule_appointment / cancel_appointment voice tools.

These exercise the Retell custom-tool payload shape ({call, name, args}) end to
end through /webhooks/retell, asserting the booking row is moved or cancelled
and an audit log is written. SMS is disabled by default in tests, so no network
call is made (send_* returns {"skipped": "sms_disabled"}).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.booking import Booking
from tests.conftest import seed_practice

# A clearly-future date so the booking counts as "upcoming" regardless of when
# the suite runs.
_FUTURE = "2099-12-15"
_FUTURE_2 = "2099-12-20"
_PHONE = "+15557770000"


async def _book(client, *, phone=_PHONE, date=_FUTURE, window="morning"):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-1", "agent_id": "agent_rc"},
            "name": "book_appointment",
            "args": {
                "patient_first_name": "Maria",
                "patient_last_name": "Lopez",
                "patient_phone": phone,
                "procedure": "cleaning",
                "preferred_date": date,
                "preferred_time_window": window,
            },
        },
    )


async def test_reschedule_moves_upcoming_booking(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Reschedule Dental", clerk_org_id="org_rc1", clerk_user_id="user_rc1"
    )
    book_resp = await _book(client)
    assert book_resp.status_code == 200 and book_resp.json()["booked"] is True

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-1", "agent_id": "agent_rc"},
            "name": "reschedule_appointment",
            "args": {
                "patient_phone": _PHONE,
                "new_date": _FUTURE_2,
                "new_time_window": "afternoon",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rescheduled"] is True
    assert body["appointment"]["date"] == _FUTURE_2

    await db_session.commit()
    bookings = (
        await db_session.execute(select(Booking).where(Booking.practice_id == practice.id))
    ).scalars().all()
    assert len(bookings) == 1  # moved, not duplicated
    assert bookings[0].appointment_at.date().isoformat() == _FUTURE_2
    assert bookings[0].status == "confirmed"

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "booking_rescheduled")
        )
    ).scalars().all()
    assert len(audits) == 1


async def test_cancel_marks_booking_cancelled(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Cancel Dental", clerk_org_id="org_rc2", clerk_user_id="user_rc2"
    )
    book_resp = await _book(client)
    assert book_resp.status_code == 200 and book_resp.json()["booked"] is True

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-1", "agent_id": "agent_rc"},
            "name": "cancel_appointment",
            "args": {"patient_phone": _PHONE, "reason": "going out of town"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True

    await db_session.commit()
    booking = (
        await db_session.execute(select(Booking).where(Booking.practice_id == practice.id))
    ).scalar_one()
    assert booking.status == "cancelled"

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "booking_cancelled")
        )
    ).scalars().all()
    assert len(audits) == 1


async def test_reschedule_unknown_phone_is_graceful(client, db_session):
    await seed_practice(
        db_session, name="NoMatch Dental", clerk_org_id="org_rc3", clerk_user_id="user_rc3"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-2", "agent_id": "agent_rc"},
            "name": "reschedule_appointment",
            "args": {"patient_phone": "+15550009999", "new_date": _FUTURE_2},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["rescheduled"] is False


async def test_cancel_unknown_phone_is_graceful(client, db_session):
    await seed_practice(
        db_session, name="NoMatch2 Dental", clerk_org_id="org_rc4", clerk_user_id="user_rc4"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-3", "agent_id": "agent_rc"},
            "name": "cancel_appointment",
            "args": {"patient_phone": "+15550008888"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False


async def test_lookup_patient_recognizes_returning(client, db_session):
    await seed_practice(
        db_session, name="Returning Dental", clerk_org_id="org_rc5", clerk_user_id="user_rc5"
    )
    book_resp = await _book(client)
    assert book_resp.status_code == 200 and book_resp.json()["booked"] is True

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-1", "agent_id": "agent_rc"},
            "name": "lookup_patient",
            "args": {"patient_phone": _PHONE},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["patient_first_name"] == "Maria"
    assert body["has_upcoming_appointment"] is True
    assert body["upcoming"]["date"] == _FUTURE


async def test_lookup_patient_unknown_returns_not_found(client, db_session):
    await seed_practice(
        db_session, name="Unknown Dental", clerk_org_id="org_rc6", clerk_user_id="user_rc6"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "rc-call-9", "agent_id": "agent_rc"},
            "name": "lookup_patient",
            "args": {"patient_phone": "+15550007777"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["found"] is False
