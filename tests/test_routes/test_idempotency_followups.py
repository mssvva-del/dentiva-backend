"""Idempotency follow-ups (audit §2): meter-once, book-once, twilio sid dedup.

Webhooks get redelivered on timeout — the SAME event twice must not double-count
minutes, create a second booking, or re-process an inbound SMS.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.models.booking import Booking
from app.models.usage_record import UsageRecord
from tests.conftest import seed_practice


async def _call_started(client, call_id):
    return await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"from_number": "+15551234567", "to_number": "+15559876543"},
    })


async def _call_ended(client, call_id):
    # 120s call (start→end), clean hangup → status completed.
    return await client.post("/webhooks/retell", json={
        "event": "call_ended", "call_id": call_id,
        "call": {
            "start_timestamp": 1748563200000, "end_timestamp": 1748563320000,
            "disconnection_reason": "user_hangup",
        },
    })


# ── 1. metering: redelivered call_ended counts minutes ONCE ──────────────────
async def test_call_ended_meters_once_on_redelivery(client, db_session):
    await seed_practice(db_session, name="Meter1", clerk_org_id="org_m1", clerk_user_id="u_m1")
    await _call_started(client, "rc-meter-1")
    await _call_ended(client, "rc-meter-1")
    await _call_ended(client, "rc-meter-1")  # redelivery

    row = (await db_session.execute(select(UsageRecord))).scalars().first()
    assert row is not None
    assert row.minutes_used == Decimal("2.00")  # 120s once, NOT 4.00
    assert row.calls_count == 1


# ── 2. booking: redelivered book_appointment creates ONE booking ─────────────
async def test_book_appointment_idempotent_on_redelivery(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Book1", clerk_org_id="org_bk9", clerk_user_id="u_bk9"
    )
    await _call_started(client, "rc-bk-9")
    args = {
        "patient_first_name": "Maria", "patient_last_name": "Garcia",
        "patient_phone": "+15551234567", "procedure": "cleaning",
        "preferred_date": "2026-06-05", "preferred_time_window": "morning",
    }
    body = {"event": "function_call", "call_id": "rc-bk-9",
            "function_name": "book_appointment", "args": args}
    r1 = await client.post("/webhooks/retell", json=body)
    r2 = await client.post("/webhooks/retell", json=body)  # redelivery
    assert r1.json()["booked"] is True
    assert r2.json()["booked"] is True

    count = (await db_session.execute(
        select(func.count()).select_from(Booking).where(Booking.practice_id == practice.id)
    )).scalar_one()
    assert count == 1, "redelivered book_appointment must not create a 2nd booking"


# ── 3. twilio: same MessageSid claimed once ──────────────────────────────────
async def test_twilio_sid_dedup(client, db_session):
    from app.webhooks.twilio_sms import _already_claimed
    assert await _already_claimed("SM_dup_1") is False   # first → claimed
    assert await _already_claimed("SM_dup_1") is True    # second → duplicate
    assert await _already_claimed("SM_other") is False   # different sid → fresh
