"""The dashboard numbers, checked against data instead of against their own shape.

The existing tests for these endpoints seed a practice with no calls and assert
that keys exist and numbers are non-negative. That is exactly the pattern that
hid the ROI bug for months: it counted missed calls as answered, and every test
passed because none of them ever put a call in the database.

So these seed a known mix and assert the figures equal what was seeded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from tests.conftest import seed_practice

_TZ = "America/New_York"


async def _call(db_session, practice_id, *, status, when):
    db_session.add(Call(
        id=uuid.uuid4(), practice_id=practice_id,
        retell_call_id=f"dash-{uuid.uuid4().hex[:8]}",
        direction="inbound", from_number="+16205551111", to_number="+15559876543",
        started_at=when, status=status, duration_seconds=120,
    ))
    await db_session.commit()


async def _get(client, path, org, user):
    r = await client.get(path, headers={
        "X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org,
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _practice(db_session, name, org, user):
    practice, _ = await seed_practice(
        db_session, name=name, clerk_org_id=org, clerk_user_id=user
    )
    practice.timezone = _TZ
    await db_session.commit()
    return practice


async def _patient(db_session, practice_id):
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice_id,
        pms_external_id=f"P-{uuid.uuid4().hex[:6]}",
        first_name="Ann", last_name="Lee", phone="+16205551111",
    )
    db_session.add(patient)
    await db_session.commit()
    return patient.id


async def test_the_weekly_chart_puts_a_call_on_the_day_it_happened(
    client, db_session
):
    """THE test. Days are the clinic's days, not UTC's.

    A call at 8pm in New York is midnight UTC the NEXT day. Grouping by the UTC
    date moves every evening call onto tomorrow's bar — so the clinic's busiest
    hours are attributed to a day they had not started yet, and today's bar is
    always missing its own evening.
    """
    practice = await _practice(db_session, "Weekly Dental", "org_wk1", "u_wk1")
    practice_id = practice.id

    # 8pm New York today. In UTC that is midnight tomorrow.
    now_local = datetime.now(UTC).astimezone()
    evening_utc = datetime.now(UTC).replace(hour=1, minute=0, second=0, microsecond=0)
    # 01:00 UTC = 21:00 the PREVIOUS day in New York.
    await _call(db_session, practice_id, status="completed", when=evening_utc)

    data = await _get(client, "/api/dashboard/weekly", "org_wk1", "u_wk1")
    days = {d["date"]: d for d in data["days"]}
    local_day = evening_utc.astimezone(
        __import__("zoneinfo").ZoneInfo(_TZ)
    ).date().isoformat()
    assert local_day in days, f"{local_day} is not in the 7-day window: {list(days)}"
    assert days[local_day]["calls_total"] == 1, (
        f"an evening call was filed under the wrong day: {days}"
    )
    assert now_local is not None  # keep the local clock referenced


async def test_weekly_totals_equal_what_was_seeded(client, db_session):
    practice = await _practice(db_session, "Weekly Dental 2", "org_wk2", "u_wk2")
    practice_id = practice.id
    midday = datetime.now(UTC).replace(hour=16, minute=0, second=0, microsecond=0)

    for _ in range(5):
        await _call(db_session, practice_id, status="completed", when=midday)
    for _ in range(2):
        await _call(db_session, practice_id, status="missed", when=midday)
    patient_id = await _patient(db_session, practice_id)
    db_session.add(Booking(
        id=uuid.uuid4(), practice_id=practice_id, patient_id=patient_id,
        appointment_at=midday + timedelta(days=3), status="confirmed",
        procedure_type="cleaning",
    ))
    await db_session.commit()

    totals = (await _get(client, "/api/dashboard/weekly", "org_wk2", "u_wk2"))["totals"]
    assert totals["calls_total"] == 7
    assert totals["calls_answered_by_ai"] == 5
    assert totals["calls_missed"] == 2
    assert totals["bookings_created"] == 1
    # Five of seven answered — not six, and not five of five.
    assert totals["ai_answer_rate"] == round(5 / 7, 3)


async def test_the_conversion_funnel_counts_what_happened(client, db_session):
    practice = await _practice(db_session, "Conv Dental", "org_cv1", "u_cv1")
    practice_id = practice.id
    midday = datetime.now(UTC).replace(hour=16, minute=0, second=0, microsecond=0)

    for _ in range(8):
        await _call(db_session, practice_id, status="completed", when=midday)
    for _ in range(2):
        await _call(db_session, practice_id, status="missed", when=midday)
    patient_id = await _patient(db_session, practice_id)
    # Distinct slots: one confirmed booking per (practice, time) is enforced by a
    # unique index, which is the double-book guard doing its job.
    for hour in range(4):
        db_session.add(Booking(
            id=uuid.uuid4(), practice_id=practice_id, patient_id=patient_id,
            appointment_at=midday + timedelta(days=2, hours=hour),
            status="confirmed", procedure_type="cleaning",
        ))
    await db_session.commit()

    data = await _get(client, "/api/dashboard/conversion", "org_cv1", "u_cv1")
    assert data["calls_total"] == 10
    assert data["calls_completed"] == 8
    assert data["bookings_created"] == 4
    assert data["conversion_rate"] == round(4 / 10, 3)
    assert data["ai_answer_rate"] == round(8 / 10, 3)


async def test_the_briefing_does_not_count_a_missed_call_as_answered(
    client, db_session
):
    practice = await _practice(db_session, "Brief Dental", "org_bf1", "u_bf1")
    practice_id = practice.id
    midday = datetime.now(UTC).replace(hour=16, minute=0, second=0, microsecond=0)

    for _ in range(3):
        await _call(db_session, practice_id, status="completed", when=midday)
    await _call(db_session, practice_id, status="missed", when=midday)

    stats = (await _get(client, "/api/dashboard/briefing", "org_bf1", "u_bf1"))["stats"]
    assert stats["calls_today"] == 4
    assert stats["calls_missed"] == 1
    assert stats["calls_answered_by_ai"] == 3
