"""Tests for the reschedule_appointment / cancel_appointment voice tools.

These exercise the Retell custom-tool payload shape ({call, name, args}) end to
end through /webhooks/retell, asserting the booking row is moved or cancelled
and an audit log is written. SMS is disabled by default in tests, so no network
call is made (send_* returns {"skipped": "sms_disabled"}).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.patient import Patient
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
    """A returning patient calling from their own number is greeted by name.

    The caller ID is what makes this safe, and it is the ordinary case: someone
    ringing the practice from the phone their record was created with. Booking on
    this call is deliberately NOT enough here — lookup only discloses, and
    creating an appointment proves nothing about whose record it is.
    """
    await seed_practice(
        db_session, name="Returning Dental", clerk_org_id="org_rc5", clerk_user_id="user_rc5"
    )
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "rc-call-1",
        "call": {"from_number": _PHONE, "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
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


# ---------------------------------------------------------------------------
# THE COMPOSITION ATTACK
#
# Booking is deliberately unauthenticated — a first-time patient must be able to
# make one. But _upsert_patient matches on phone number alone, so "booking as a
# patient" attaches to whatever record already holds that number.
#
# An attacker who knows only a phone number could therefore: book (attaching to
# the victim's record), be recognised as "the person who booked on this call",
# and then cancel — because cancel takes the SOONEST upcoming appointment, which
# is the victim's real one, not the decoy just created.
#
# The rule was written from the honest case (booked, then changed their mind) and
# passes it. It also passed an attacker performing exactly those steps, because
# the qualifying fact is one the attacker manufactures.
# ---------------------------------------------------------------------------

VICTIM_PHONE = "+15558881234"


async def _victim_with_appointment(db_session, practice_id):
    from app.utils.crypto import phone_hmac

    victim = Patient(
        id=uuid.uuid4(), practice_id=practice_id, pms_external_id="EXT-VICTIM",
        first_name="Maria", last_name="Vega", phone=VICTIM_PHONE,
        phone_hmac=phone_hmac(VICTIM_PHONE), date_of_birth="1984-03-07",
    )
    db_session.add(victim)
    await db_session.flush()
    real = Booking(
        id=uuid.uuid4(), practice_id=practice_id, patient_id=victim.id,
        appointment_at=datetime.now(tz=UTC) + timedelta(days=2),
        status="confirmed", procedure_type="cleaning", provider_name="Dr. Smith",
        source="ai_call",
    )
    db_session.add(real)
    await db_session.commit()
    return real.id


async def _attacker_books_as_victim(client, call_id: str):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"from_number": "+15559990000", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Whoever", "patient_last_name": "Attacker",
                 "patient_phone": VICTIM_PHONE, "procedure": "cleaning",
                 "preferred_date": "2099-12-20", "preferred_time_window": "morning"},
    })
    assert r.json().get("booked") is True, "booking is unauthenticated by design"


async def test_booking_on_this_call_does_not_unlock_an_earlier_appointment(
    client, db_session
):
    practice, _ = await seed_practice(
        db_session, name="Attack Dental", clerk_org_id="org_atk1", clerk_user_id="user_atk1"
    )
    practice_id = practice.id
    real_id = await _victim_with_appointment(db_session, practice_id)

    await _attacker_books_as_victim(client, "atk-1")
    await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "atk-1",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": VICTIM_PHONE, "reason": "changed my mind"},
    })

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    status = (await db_session.execute(
        select(Booking.status).where(Booking.id == real_id)
    )).scalar_one()
    assert status == "confirmed", "a stranger cancelled someone else's appointment"


async def test_the_same_applies_to_rescheduling(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Attack Dental 2", clerk_org_id="org_atk2", clerk_user_id="user_atk2"
    )
    practice_id = practice.id
    real_id = await _victim_with_appointment(db_session, practice_id)
    before = (await db_session.execute(
        select(Booking.appointment_at).where(Booking.id == real_id)
    )).scalar_one()

    await _attacker_books_as_victim(client, "atk-2")
    await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "atk-2",
        "function_name": "reschedule_appointment",
        "args": {"patient_phone": VICTIM_PHONE, "new_date": "2099-12-28"},
    })

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    after = (await db_session.execute(
        select(Booking.appointment_at).where(Booking.id == real_id)
    )).scalar_one()
    assert after == before, "a stranger moved someone else's appointment"


async def test_a_lookup_needs_more_than_a_booking_made_on_this_call(client, db_session):
    """lookup_patient only discloses. Creating an appointment proves nothing about
    who the record belongs to, so it must not open the record."""
    practice, _ = await seed_practice(
        db_session, name="Attack Dental 3", clerk_org_id="org_atk3", clerk_user_id="user_atk3"
    )
    await _victim_with_appointment(db_session, practice.id)
    await _attacker_books_as_victim(client, "atk-3")

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "atk-3",
        "function_name": "lookup_patient",
        "args": {"patient_phone": VICTIM_PHONE},
    })
    body = r.json()
    assert body["found"] is False
    assert "Maria" not in str(body), "the victim's name must not leak"


async def test_the_honest_caller_can_still_undo_what_they_just_booked(
    client, db_session
):
    """The case the rule exists for: a patient books and changes their mind two
    sentences later, from a phone we have never seen, with no date of birth on
    file. Narrowing the grant must not take that away."""
    practice, _ = await seed_practice(
        db_session, name="Honest Dental", clerk_org_id="org_hon1", clerk_user_id="user_hon1"
    )
    practice_id = practice.id

    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "hon-1",
        "call": {"from_number": "+15551110000", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "hon-1",
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Dana", "patient_last_name": "Reed",
                 "patient_phone": "+15552223333", "procedure": "cleaning",
                 "preferred_date": "2099-11-10", "preferred_time_window": "morning"},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "hon-1",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": "+15552223333", "reason": "actually I can't make it"},
    })
    assert r.json()["cancelled"] is True

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    statuses = (await db_session.execute(select(Booking.status))).scalars().all()
    assert statuses == ["cancelled"]


# ---------------------------------------------------------------------------
# One phone, several people. This is not an edge case in dentistry — a household
# line, a couple, parents and children all sit on one number, and the practice
# registers them over years. Taking the OLDEST match meant the family's
# first-registered patient absorbed everyone else's calls.
# ---------------------------------------------------------------------------

_FAMILY = "+15557778888"


async def _household(db_session, practice_id, *people):
    """people: (first_name, dob) — created in order, so the first is the oldest
    record and the one the old code would always have returned."""
    from app.utils.crypto import phone_hmac

    ids = {}
    for first, dob in people:
        patient = Patient(
            id=uuid.uuid4(), practice_id=practice_id,
            pms_external_id=f"EXT-{first.upper()}",
            first_name=first, last_name="Vega", phone=_FAMILY,
            phone_hmac=phone_hmac(_FAMILY), date_of_birth=dob,
        )
        db_session.add(patient)
        ids[first] = patient.id
    await db_session.flush()
    return ids


async def test_a_second_family_member_gets_their_own_record(client, db_session):
    """The wife registered years ago. The husband calls the same practice from
    the same phone and books. He must not land on her chart."""
    practice, _ = await seed_practice(
        db_session, name="Family Dental", clerk_org_id="org_fam1", clerk_user_id="user_fam1"
    )
    practice_id = practice.id
    await _household(db_session, practice_id, ("Maria", "1984-03-07"))
    await db_session.commit()

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "fam-1",
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Diego", "patient_last_name": "Vega",
                 "patient_phone": _FAMILY, "procedure": "cleaning",
                 "preferred_date": "2099-11-10", "preferred_time_window": "morning"},
    })
    assert r.json().get("booked") is True

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    names = sorted((await db_session.execute(select(Patient.first_name))).scalars().all())
    assert names == ["Diego", "Maria"], "the husband was filed under his wife"


async def test_cancelling_from_a_shared_number_asks_instead_of_guessing(
    client, db_session
):
    """Two people, one number, no way to tell them apart. Silently taking either
    one's appointment is not a wrong record — it is the wrong person's medical
    appointment."""
    practice, _ = await seed_practice(
        db_session, name="Family Dental 2", clerk_org_id="org_fam2", clerk_user_id="user_fam2"
    )
    practice_id = practice.id
    ids = await _household(
        db_session, practice_id, ("Maria", "1984-03-07"), ("Diego", "1981-09-14")
    )
    for first in ("Maria", "Diego"):
        db_session.add(Booking(
            id=uuid.uuid4(), practice_id=practice_id, patient_id=ids[first],
            appointment_at=datetime.now(tz=UTC) + timedelta(days=2 if first == "Maria" else 5),
            status="confirmed", procedure_type="cleaning", provider_name="Dr. Smith",
            source="ai_call",
        ))
    await db_session.commit()

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "fam-2",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": _FAMILY, "reason": "can't make it"},
    })
    body = r.json()
    assert body["cancelled"] is False
    assert "date of birth" in body["message"]

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    statuses = (await db_session.execute(select(Booking.status))).scalars().all()
    assert statuses == ["confirmed", "confirmed"], "someone's appointment was cancelled"


async def test_the_date_of_birth_resolves_the_household(client, db_session):
    """Having asked, the answer must actually work — otherwise the caller is in a
    loop and the front desk gets the call anyway."""
    practice, _ = await seed_practice(
        db_session, name="Family Dental 3", clerk_org_id="org_fam3", clerk_user_id="user_fam3"
    )
    practice_id = practice.id
    ids = await _household(
        db_session, practice_id, ("Maria", "1984-03-07"), ("Diego", "1981-09-14")
    )
    diego_booking = uuid.uuid4()
    db_session.add(Booking(
        id=diego_booking, practice_id=practice_id, patient_id=ids["Diego"],
        appointment_at=datetime.now(tz=UTC) + timedelta(days=5),
        status="confirmed", procedure_type="cleaning", provider_name="Dr. Smith",
        source="ai_call",
    ))
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice_id, patient_id=ids["Maria"],
        appointment_at=datetime.now(tz=UTC) + timedelta(days=2),
        status="confirmed", procedure_type="cleaning", provider_name="Dr. Smith",
        source="ai_call",
    ))
    await db_session.commit()

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "fam-3",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": _FAMILY, "patient_dob": "1981-09-14",
                 "reason": "can't make it"},
    })
    assert r.json()["cancelled"] is True

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    status = (await db_session.execute(
        select(Booking.status).where(Booking.id == diego_booking)
    )).scalar_one()
    assert status == "cancelled", "the wrong household member's appointment moved"


async def test_a_lone_patient_on_their_number_is_untouched(client, db_session):
    """The common case must not pay for the rare one: one person, one number, no
    extra question."""
    practice, _ = await seed_practice(
        db_session, name="Family Dental 4", clerk_org_id="org_fam4", clerk_user_id="user_fam4"
    )
    ids = await _household(db_session, practice.id, ("Maria", "1984-03-07"))
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=ids["Maria"],
        appointment_at=datetime.now(tz=UTC) + timedelta(days=2),
        status="confirmed", procedure_type="cleaning", provider_name="Dr. Smith",
        source="ai_call",
    ))
    await db_session.commit()

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "fam-4",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": _FAMILY, "patient_dob": "1984-03-07",
                 "reason": "can't make it"},
    })
    assert r.json()["cancelled"] is True


# ---------------------------------------------------------------------------
# The clinic's own calendar. Cancel and move stopped at our database, so the
# front desk kept seeing a patient who had cancelled, and after a move held the
# old time while knowing nothing of the new one — one call, two wrong entries.
# ---------------------------------------------------------------------------


async def _booked_with_a_pms_record(db_session, practice_id, when):
    from app.models.patient import Patient
    from app.utils.crypto import phone_hmac

    phone = "+15554443333"
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice_id, pms_external_id="EXT-PMS",
        first_name="Dana", last_name="Reed", phone=phone, phone_hmac=phone_hmac(phone),
        date_of_birth="1990-01-01",
    )
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice_id, patient_id=patient.id,
        appointment_at=when, status="confirmed", procedure_type="cleaning",
        provider_name="Dr. Smith", source="ai_call",
        pms_external_id="appointments/77",
    )
    db_session.add(booking)
    await db_session.commit()
    return phone, booking.id


async def test_cancelling_reaches_the_clinics_calendar(client, db_session, monkeypatch):
    """Without this the chair stays blocked: the patient is gone and the front
    desk still has them booked, which is the revenue loss this product is sold
    to prevent, running backwards."""
    from app.services.reactivation import writeback

    practice, _ = await seed_practice(
        db_session, name="PMS Cancel", clerk_org_id="org_pc1", clerk_user_id="user_pc1"
    )
    phone, _ = await _booked_with_a_pms_record(
        db_session, practice.id, datetime.now(tz=UTC) + timedelta(days=3)
    )

    cancelled: list[str] = []

    class _Client:
        async def cancel_appointment(self, appointment_id, **kwargs):
            cancelled.append(appointment_id)

    monkeypatch.setattr(
        writeback, "_bridge_for",
        lambda session, practice_id, client=None: _returns(_Client()),
    )

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "pms-c1",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": phone, "patient_dob": "1990-01-01",
                 "reason": "can't make it"},
    })
    assert r.json()["cancelled"] is True
    assert cancelled == ["appointments/77"]


async def test_a_booking_that_never_reached_the_pms_is_not_an_error(
    client, db_session, monkeypatch
):
    """Most bookings today have no PMS record at all. Treating that as a failed
    cancellation would alert on nearly every call and bury the ones that matter."""
    from app.services.reactivation import writeback

    practice, _ = await seed_practice(
        db_session, name="PMS Cancel 2", clerk_org_id="org_pc2", clerk_user_id="user_pc2"
    )
    practice_id = practice.id
    phone, booking_id = await _booked_with_a_pms_record(
        db_session, practice_id, datetime.now(tz=UTC) + timedelta(days=3)
    )
    await db_session.execute(
        Booking.__table__.update().where(Booking.id == booking_id)
        .values(pms_external_id=None)
    )
    await db_session.commit()

    called: list[str] = []

    class _Client:
        async def cancel_appointment(self, appointment_id, **kwargs):
            called.append(appointment_id)

    monkeypatch.setattr(
        writeback, "_bridge_for",
        lambda session, practice_id, client=None: _returns(_Client()),
    )

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "pms-c2",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": phone, "patient_dob": "1990-01-01"},
    })
    assert r.json()["cancelled"] is True
    assert called == [], "asked the PMS to cancel something it never had"


async def test_a_pms_that_refuses_the_cancel_does_not_undo_ours(
    client, db_session, monkeypatch
):
    """The patient has been told it is cancelled and the waitlist has already
    been offered the slot. Rolling our side back to match the PMS would be a
    second wrong answer, not a correction."""
    from app.adapters.kolla.client import KollaError
    from app.services.reactivation import writeback

    practice, _ = await seed_practice(
        db_session, name="PMS Cancel 3", clerk_org_id="org_pc3", clerk_user_id="user_pc3"
    )
    practice_id = practice.id
    phone, booking_id = await _booked_with_a_pms_record(
        db_session, practice_id, datetime.now(tz=UTC) + timedelta(days=3)
    )

    class _Client:
        async def cancel_appointment(self, appointment_id, **kwargs):
            raise KollaError("the PMS said no")

    monkeypatch.setattr(
        writeback, "_bridge_for",
        lambda session, practice_id, client=None: _returns(_Client()),
    )

    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "pms-c3",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": phone, "patient_dob": "1990-01-01"},
    })
    assert r.json()["cancelled"] is True

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    assert (await db_session.execute(
        select(Booking.status).where(Booking.id == booking_id)
    )).scalar_one() == "cancelled"


async def _returns(value):
    return value


async def test_the_patient_is_told_the_date_that_was_actually_booked(
    client, db_session
):
    """compute_native_slots treats the requested date as a STARTING POINT and
    scans forward two weeks. Ask for a day the clinic is closed and the move
    lands later — while the agent said, and the text confirmed, the day that was
    asked for.

    The patient then arrives at a practice that is not expecting them, and the
    clinic blames the AI. book_appointment was fixed for exactly this; reschedule
    kept speaking the request.
    """
    practice, _ = await seed_practice(
        db_session, name="Spoken Date", clerk_org_id="org_sd1", clerk_user_id="user_sd1"
    )
    practice_id = practice.id
    phone, booking_id = await _booked_with_a_pms_record(
        db_session, practice_id, datetime.now(tz=UTC) + timedelta(days=2)
    )

    # A Sunday. seed_practice opens Monday to Friday, so nothing is free that day.
    a_sunday = "2099-11-15"
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "spoken-1",
        "function_name": "reschedule_appointment",
        "args": {"patient_phone": phone, "patient_dob": "1990-01-01",
                 "new_date": a_sunday},
    })
    body = r.json()
    if not body.get("rescheduled"):
        pytest.skip("no slot offered at all — a different branch, covered elsewhere")

    spoken = body["appointment"]["date"]
    assert spoken != a_sunday, "told the patient a day the clinic is closed"

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    stored = (await db_session.execute(
        select(Booking.appointment_at).where(Booking.id == booking_id)
    )).scalar_one()
    assert stored.date().isoformat() == spoken, "spoken date and stored date differ"
    assert spoken in body["message"], "the sentence and the record disagree"
