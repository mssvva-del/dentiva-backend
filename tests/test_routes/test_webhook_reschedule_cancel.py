"""Tests for the reschedule_appointment / cancel_appointment voice tools.

These exercise the Retell custom-tool payload shape ({call, name, args}) end to
end through /webhooks/retell, asserting the booking row is moved or cancelled
and an audit log is written. SMS is disabled by default in tests, so no network
call is made (send_* returns {"skipped": "sms_disabled"}).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.booking import Booking
from tests.conftest import seed_practice

# Clearly-future WEEKDAYS (native availability only offers open business days —
# 2099-12-15 is a Tuesday, 2099-12-18 a Friday; both within Mon–Fri hours).
_FUTURE = "2099-12-15"
_FUTURE_2 = "2099-12-18"
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


# ---------------------------------------------------------------------------
# CROSS-TENANT PHONE COLLISION
#
# A phone number is not a unique key ACROSS practices — the same person (or a
# shared family line) can be a patient at two different Dentovox clinics. Every
# lookup here is scoped by (practice_id, phone), so a call bound to clinic A must
# never surface or touch clinic B's same-number patient, even though the phone
# digits collide exactly. This is the scenario the property-based RLS test
# doesn't reach (that test asserts on raw table rows; this drives the actual
# voice-tool code path — the phone_hmac lookup — end to end).
# ---------------------------------------------------------------------------

_SHARED_PHONE = "+15551119999"


async def test_reschedule_cancel_lookup_never_cross_the_phone_collision(client, db_session):
    practice_a, _ = await seed_practice(
        db_session, name="Collide A", clerk_org_id="org_col_a", clerk_user_id="user_col_a"
    )
    practice_b, _ = await seed_practice(
        db_session, name="Collide B", clerk_org_id="org_col_b", clerk_user_id="user_col_b"
    )
    practice_a.retell_agent_id = "agent_col_a"
    practice_b.retell_agent_id = "agent_col_b"
    await db_session.commit()

    # Same phone number books at BOTH clinics, on different days.
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "col-call-a",
        "call": {"agent_id": "agent_col_a", "from_number": _SHARED_PHONE,
                 "to_number": "+15550000001"},
    })
    book_a = await client.post("/webhooks/retell", json={
        "call": {"call_id": "col-call-a", "agent_id": "agent_col_a"},
        "name": "book_appointment",
        "args": {"patient_first_name": "Pat", "patient_last_name": "A",
                 "patient_phone": _SHARED_PHONE, "procedure": "cleaning",
                 "preferred_date": _FUTURE, "preferred_time_window": "morning"},
    })
    assert book_a.json()["booked"] is True

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "col-call-b",
        "call": {"agent_id": "agent_col_b", "from_number": _SHARED_PHONE,
                 "to_number": "+15550000002"},
    })
    book_b = await client.post("/webhooks/retell", json={
        "call": {"call_id": "col-call-b", "agent_id": "agent_col_b"},
        "name": "book_appointment",
        "args": {"patient_first_name": "Pat", "patient_last_name": "B",
                 "patient_phone": _SHARED_PHONE, "procedure": "cleaning",
                 "preferred_date": _FUTURE_2, "preferred_time_window": "morning"},
    })
    assert book_b.json()["booked"] is True

    # lookup_patient on clinic A's call must return A's own appointment, not B's.
    lookup = await client.post("/webhooks/retell", json={
        "call": {"call_id": "col-call-a", "agent_id": "agent_col_a"},
        "name": "lookup_patient",
        "args": {"patient_phone": _SHARED_PHONE},
    })
    assert lookup.json()["patient_first_name"] == "Pat"
    assert lookup.json()["upcoming"]["date"] == _FUTURE, "must see A's slot, not B's"

    # Reschedule on clinic A's call must move ONLY clinic A's booking.
    resched = await client.post("/webhooks/retell", json={
        "call": {"call_id": "col-call-a", "agent_id": "agent_col_a"},
        "name": "reschedule_appointment",
        "args": {"patient_phone": _SHARED_PHONE, "new_date": "2099-12-22",
                  "new_time_window": "afternoon"},
    })
    assert resched.json()["rescheduled"] is True

    await db_session.commit()
    from app.db import set_tenant
    from app.models.patient import Patient

    await set_tenant(db_session, practice_a.id)
    booking_a = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice_a.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert booking_a.appointment_at.date().isoformat() == "2099-12-22"

    await set_tenant(db_session, practice_b.id)
    booking_b = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice_b.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert booking_b.appointment_at.date().isoformat() == _FUTURE_2, (
        "clinic B's booking must be untouched by a reschedule on clinic A's call"
    )
    assert booking_b.status == "confirmed"

    # Cancel on clinic A's call must cancel ONLY clinic A's booking.
    cancel = await client.post("/webhooks/retell", json={
        "call": {"call_id": "col-call-a", "agent_id": "agent_col_a"},
        "name": "cancel_appointment",
        "args": {"patient_phone": _SHARED_PHONE, "reason": "reschedule test"},
    })
    assert cancel.json()["cancelled"] is True

    await db_session.commit()
    await set_tenant(db_session, practice_a.id)
    booking_a = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice_a.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert booking_a.status == "cancelled"

    await set_tenant(db_session, practice_b.id)
    booking_b = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice_b.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert booking_b.status == "confirmed", (
        "clinic B's booking must still be confirmed — clinic A's cancel must not reach it"
    )

    # And each clinic has exactly ONE patient row for this phone — no cross-tenant
    # duplicate/merge happened during the upserts above.
    await set_tenant(db_session, practice_a.id)
    patients_a = (await db_session.execute(
        select(Patient).where(Patient.practice_id == practice_a.id)
    )).scalars().all()
    assert len(patients_a) == 1 and patients_a[0].last_name == "A"

    await set_tenant(db_session, practice_b.id)
    patients_b = (await db_session.execute(
        select(Patient).where(Patient.practice_id == practice_b.id)
    )).scalars().all()
    assert len(patients_b) == 1 and patients_b[0].last_name == "B"


# ---------------------------------------------------------------------------
# WHO IS ASKING
#
# These three tools took a phone number from the model's tool call and looked the
# record up — nothing compared it to the line the caller was actually on, and the
# prompt's "verify name and date of birth" had nowhere to land because the tool
# schemas had no DOB field. "Check my ex's appointment, her number is …" returned
# her name and visit time; "reschedule it" moved it.
# ---------------------------------------------------------------------------


async def _seed_patient_with_booking(db_session, practice_id, *, phone, dob, when):
    from app.models.booking import Booking
    from app.models.patient import Patient
    from app.utils.crypto import phone_hmac

    patient = Patient(
        id=uuid.uuid4(), practice_id=practice_id, pms_external_id=f"T-{uuid.uuid4().hex[:6]}",
        first_name="Maria", last_name="Vega", phone=phone, phone_hmac=phone_hmac(phone),
        date_of_birth=dob,
    )
    db_session.add(patient)
    await db_session.flush()
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice_id, patient_id=patient.id,
        appointment_at=when, status="confirmed", procedure_type="cleaning",
        provider_name="Dr. Smith", source="ai_call",
    ))
    await db_session.commit()
    return patient


async def test_a_stranger_cannot_look_up_someone_elses_appointment(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Privacy Dental", clerk_org_id="org_priv1", clerk_user_id="user_priv1"
    )
    await _seed_patient_with_booking(
        db_session, practice.id, phone="+15558887777", dob="1984-03-07",
        when=datetime.now(tz=UTC) + timedelta(days=3),
    )
    # The call comes from a DIFFERENT line, and no date of birth is offered.
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "priv-1",
        "call": {"from_number": "+15551234444", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "priv-1",
        "function_name": "lookup_patient",
        "args": {"patient_phone": "+15558887777"},
    })
    body = r.json()
    assert body["found"] is False, "must not confirm the record even exists"
    assert body["verify_identity"] is True
    assert "Maria" not in str(body), "no name may leak in the refusal"


async def test_the_date_of_birth_unlocks_it(client, db_session):
    """A patient calling from a different phone is normal — a spouse's mobile, a
    work line. The date of birth is what a receptionist would ask for."""
    practice, _ = await seed_practice(
        db_session, name="Privacy Dental 2", clerk_org_id="org_priv2", clerk_user_id="user_priv2"
    )
    await _seed_patient_with_booking(
        db_session, practice.id, phone="+15558886666", dob="1984-03-07",
        when=datetime.now(tz=UTC) + timedelta(days=3),
    )
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "priv-2",
        "call": {"from_number": "+15551235555", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "priv-2",
        "function_name": "lookup_patient",
        # Said aloud, transcribed loosely — format must not decide access.
        "args": {"patient_phone": "+15558886666", "patient_dob": "March 7, 1984"},
    })
    body = r.json()
    assert body["found"] is True
    assert body["patient_first_name"] == "Maria"


async def test_calling_from_your_own_number_needs_no_challenge(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Privacy Dental 3", clerk_org_id="org_priv3", clerk_user_id="user_priv3"
    )
    await _seed_patient_with_booking(
        db_session, practice.id, phone="+15558885555", dob="1990-01-02",
        when=datetime.now(tz=UTC) + timedelta(days=3),
    )
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "priv-3",
        "call": {"from_number": "+15558885555", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "priv-3",
        "function_name": "lookup_patient",
        "args": {"patient_phone": "+1 (555) 888-5555"},  # same line, written differently
    })
    assert r.json()["found"] is True


async def test_a_stranger_cannot_cancel_someone_elses_appointment(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Privacy Dental 4", clerk_org_id="org_priv4", clerk_user_id="user_priv4"
    )
    await _seed_patient_with_booking(
        db_session, practice.id, phone="+15558884444", dob="1975-11-30",
        when=datetime.now(tz=UTC) + timedelta(days=2),
    )
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "priv-4",
        "call": {"from_number": "+15559990000", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "priv-4",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": "+15558884444", "reason": "changed my mind"},
    })
    assert r.json()["cancelled"] is False
    assert r.json()["verify_identity"] is True

    # And the appointment is still standing. Read the status inside the query so
    # nothing is lazy-loaded off an expired instance.
    from app.models.booking import Booking
    await db_session.commit()
    db_session.expire_all()
    statuses = (await db_session.execute(select(Booking.status))).scalars().all()
    assert statuses == ["confirmed"], "a refused identity check must change nothing"
