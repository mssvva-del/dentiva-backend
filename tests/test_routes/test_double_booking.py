"""Double-booking guard + readiness probe."""

from __future__ import annotations

from sqlalchemy import select

from app.models.booking import Booking
from tests.conftest import seed_practice

_FUTURE = "2099-12-15"
_PHONE_A = "+15557770001"
_PHONE_B = "+15557770002"


async def _book(client, *, phone, call_id="db-call"):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": call_id, "agent_id": "agent_db"},
            "name": "book_appointment",
            "args": {
                "patient_first_name": "Pat",
                "patient_phone": phone,
                "procedure": "cleaning",
                "preferred_date": _FUTURE,
                "preferred_time_window": "morning",
            },
        },
    )


async def test_readiness_probe_ok(client, db_session):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


async def test_no_two_confirmed_bookings_same_slot(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="DoubleBook Dental", clerk_org_id="org_db1", clerk_user_id="user_db1"
    )
    # Two DIFFERENT callers (distinct call_ids) competing for the same day — same
    # call_id would now be idempotent (one booking), which isn't what we're testing.
    r1 = await _book(client, phone=_PHONE_A, call_id="db-call-a")
    assert r1.status_code == 200 and r1.json()["booked"] is True
    first_slot = r1.json()["appointment"]["time"]

    # Second patient, same day — must NOT land on the same provider+time as #1.
    r2 = await _book(client, phone=_PHONE_B, call_id="db-call-b")
    assert r2.status_code == 200
    body2 = r2.json()

    await db_session.commit()
    confirmed = (
        await db_session.execute(
            select(Booking.appointment_at, Booking.provider_name)
            .where(Booking.practice_id == practice.id, Booking.status == "confirmed")
        )
    ).all()
    # No two confirmed bookings share the same (time, provider).
    keys = [(at.isoformat(), prov) for at, prov in confirmed]
    assert len(keys) == len(set(keys)), f"double-booked: {keys}"

    # If a different slot was free, #2 booked it; if not, it declined gracefully.
    if body2.get("booked"):
        assert body2["appointment"]["time"] != first_slot or len(keys) == 2
