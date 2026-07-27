"""DB-level double-book guard — the partial-unique indexes on bookings.

App-side availability can race; these constraints are the real backstop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import set_tenant
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from tests.conftest import seed_practice

_AT = datetime(2099, 12, 15, 15, 0, tzinfo=UTC)  # future, tz-aware


async def _patient(db_session, practice):
    await set_tenant(db_session, practice.id)
    p = Patient(id=uuid.uuid4(), practice_id=practice.id,
                pms_external_id=f"ext-{uuid.uuid4().hex[:8]}",
                first_name="A", last_name="B", phone="+15550000000")
    db_session.add(p)
    await db_session.flush()
    return p


def _booking(practice, patient, *, at=_AT, status="confirmed", call_id=None):
    return Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        source_call_id=call_id, appointment_at=at, duration_minutes=60,
        procedure_type="cleaning", status=status, source="ai_call",
    )


async def _prac(db_session, slug):
    practice, _ = await seed_practice(
        db_session, name=slug, clerk_org_id=f"o_{slug}", clerk_user_id=f"u_{slug}")
    return practice


async def test_same_slot_confirmed_is_rejected(db_session):
    practice = await _prac(db_session, "dbk")
    pat = await _patient(db_session, practice)
    db_session.add(_booking(practice, pat))
    await db_session.flush()
    db_session.add(_booking(practice, pat))  # same (practice, appointment_at), confirmed
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_cancelled_does_not_collide(db_session):
    practice = await _prac(db_session, "dbk2")
    pat = await _patient(db_session, practice)
    db_session.add(_booking(practice, pat, status="cancelled"))
    db_session.add(_booking(practice, pat, status="confirmed"))  # only confirmed collide
    await db_session.flush()  # must NOT raise


async def test_one_call_one_confirmed_booking(db_session):
    practice = await _prac(db_session, "dbk3")
    pat = await _patient(db_session, practice)
    call = Call(id=uuid.uuid4(), practice_id=practice.id, retell_call_id="rc-dbk",
                direction="inbound", from_number="+15550000000", to_number="+15559999999",
                started_at=datetime.now(tz=UTC), status="completed")
    db_session.add(call)
    await db_session.flush()
    db_session.add(_booking(practice, pat, at=_AT, call_id=call.id))
    await db_session.flush()
    # different slot, SAME source call → still rejected (one call → one booking)
    other = datetime(2099, 12, 16, 9, 0, tzinfo=UTC)
    db_session.add(_booking(practice, pat, at=other, call_id=call.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_bad_booking_status_rejected(db_session):
    practice = await _prac(db_session, "dbk4")
    pat = await _patient(db_session, practice)
    db_session.add(_booking(practice, pat, status="garbage"))  # not in BOOKING_STATUSES
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_bad_call_outcome_rejected(db_session):
    practice = await _prac(db_session, "dbk5")
    await set_tenant(db_session, practice.id)
    call = Call(id=uuid.uuid4(), practice_id=practice.id, retell_call_id="rc-oc",
                direction="inbound", from_number="+15550000000", to_number="+15559999999",
                started_at=datetime.now(tz=UTC), status="completed", outcome="not_a_taxonomy")
    db_session.add(call)
    with pytest.raises(IntegrityError):
        await db_session.flush()
