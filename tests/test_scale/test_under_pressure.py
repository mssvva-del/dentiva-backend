"""What happens when several things arrive at once, or arrive twice, or arrive wrong.

Everything here is a failure a clinic would meet on an ordinary busy morning and
would never see in a quiet test: two callers reaching for the same ten o'clock,
Retell retrying a tool call it already delivered, a payload shaped like nothing
we expected, and a dozen clinics ringing at the same second.

None of these can be caught by calling the endpoints one at a time, which is how
every other test in this file tree calls them.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select

from app.models.booking import Booking
from app.models.call import Call
from tests.conftest import seed_practice


def _tool(name: str, call_id: str, args: dict, practice_id: str) -> dict:
    """A Retell custom-tool payload. metadata carries the clinic, exactly as the
    inbound webhook now answers so later events do not have to re-derive it."""
    return {
        "name": name,
        "call": {
            "call_id": call_id,
            "from_number": "+15551110000",
            "to_number": "+15559990000",
            "metadata": {"practice_id": practice_id},
        },
        "args": args,
    }


# ── Two callers, one slot ────────────────────────────────────────────────────
async def test_two_callers_cannot_take_the_same_slot(client, db_session):
    """A busy Monday: two people ask for ten o'clock within the same second.

    One of them has to be told no. Both being told yes means two strangers in one
    chair, and the practice finds out when the second one walks in.
    """
    practice, _ = await seed_practice(
        db_session, name="Race Dental", clerk_org_id="org_race", clerk_user_id="u_race"
    )
    pid = str(practice.id)
    when = "2099-06-01"

    async def book(who: str) -> dict:
        r = await client.post("/webhooks/retell", json=_tool(
            "book_appointment", f"race-{who}",
            {
                "patient_first_name": who, "patient_last_name": "Caller",
                "patient_phone": f"+1555111{who[-4:]}",
                "preferred_date": when, "preferred_time": "10:00",
                "procedure": "cleaning",
            },
            pid,
        ))
        return r.json()

    results = await asyncio.gather(book("0001"), book("0002"))

    rows = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice.id,
                              Booking.status == "confirmed")
    )).scalars().all()

    # Exactly one, not "at most one": zero would mean the booking path never ran,
    # which reads identical to a guard that worked.
    assert len(rows) == 1, f"{len(rows)} confirmed bookings for one slot"
    times = {(r.appointment_at.isoformat()) for r in rows}
    assert len(times) == 1, f"two people, one chair: {times}"
    told_yes = [r for r in results if r.get("booked")]
    assert told_yes, "nobody was told they had a slot, so nothing was tested"


# ── Retell delivers the same tool call twice ─────────────────────────────────
async def test_a_retried_tool_call_does_not_book_twice(client, db_session):
    """Retell retries on a timeout or a 5xx. The caller said "yes" once."""
    practice, _ = await seed_practice(
        db_session, name="Retry Dental", clerk_org_id="org_retry", clerk_user_id="u_retry"
    )
    payload = _tool(
        "book_appointment", "retry-same-call",
        {
            "patient_first_name": "Dana", "patient_last_name": "Twice",
            "patient_phone": "+15551119999",
            "preferred_date": "2099-06-02", "preferred_time": "09:00",
            "procedure": "cleaning",
        },
        str(practice.id),
    )

    first = (await client.post("/webhooks/retell", json=payload)).json()
    again = (await client.post("/webhooks/retell", json=payload)).json()

    rows = (await db_session.execute(
        select(func.count()).select_from(Booking)
        .where(Booking.practice_id == practice.id, Booking.status == "confirmed")
    )).scalar_one()
    assert rows == 1, f"{rows} bookings from one delivered-twice tool call"

    # And the second reply must name the SAME time. It reads the stored row back,
    # and the row is UTC: formatting it straight out told a Massachusetts patient
    # that nine in the morning was one in the afternoon.
    assert first["appointment"] == again["appointment"], (
        f"one booking described two ways: {first['appointment']} vs "
        f"{again['appointment']}"
    )


# ── A dozen clinics ringing at once ──────────────────────────────────────────
async def test_a_wave_of_calls_keeps_every_clinic_to_itself(client, db_session):
    """Twelve clinics, twelve simultaneous calls. Each call must land on the
    practice that was rung and nowhere else — the failure that put a live
    patient's booking into a different practice's dashboard tonight."""
    practices = []
    for i in range(12):
        p, _ = await seed_practice(
            db_session, name=f"Wave {i}",
            clerk_org_id=f"org_wave_{i}", clerk_user_id=f"u_wave_{i}",
        )
        practices.append(p)

    async def ring(idx: int, p) -> None:
        await client.post("/webhooks/retell", json=_tool(
            "create_callback_request", f"wave-{idx}",
            {"patient_first_name": f"P{idx}", "patient_phone": f"+1555200{idx:04d}",
             "reason": "wave"},
            str(p.id),
        ))

    await asyncio.gather(*(ring(i, p) for i, p in enumerate(practices)))

    for i, p in enumerate(practices):
        rows = (await db_session.execute(
            select(Call).where(Call.practice_id == p.id)
        )).scalars().all()
        for row in rows:
            assert row.retell_call_id == f"wave-{i}", (
                f"{p.name} is holding a call that belongs to another clinic: "
                f"{row.retell_call_id}"
            )


# ── Payloads nobody designed for ─────────────────────────────────────────────
async def test_nothing_we_can_be_sent_returns_a_500(client, db_session):
    """A 500 mid-conversation is the agent going silent on a patient. Whatever
    arrives — truncated, mistyped, enormous, or hostile — must come back as a
    refusal we chose, not a stack trace we didn't."""
    practice, _ = await seed_practice(
        db_session, name="Junk Dental", clerk_org_id="org_junk", clerk_user_id="u_junk"
    )
    pid = str(practice.id)

    payloads = [
        {},
        {"event": None},
        {"event": "call_started"},                       # no call object at all
        {"event": "call_started", "call": "not-an-object"},
        {"event": "unheard_of_event", "call_id": "x"},
        {"name": "book_appointment"},                    # a tool call with no args
        {"name": "no_such_tool", "call": {"call_id": "z"}, "args": {}},
        _tool("book_appointment", "junk-1", {"preferred_date": "not-a-date"}, pid),
        _tool("book_appointment", "junk-2", {"preferred_date": 12345}, pid),
        _tool("book_appointment", "junk-3",
              {"patient_first_name": "A" * 5000, "preferred_date": "2099-01-01"}, pid),
        _tool("lookup_patient", "junk-4", {"patient_phone": "'; DROP TABLE calls;--"}, pid),
        _tool("lookup_patient", "junk-5", {"patient_phone": "☎️🦷" * 200}, pid),
        _tool("cancel_appointment", "junk-6", {"patient_phone": None}, pid),
        _tool("check_availability", "junk-7", {"procedure": {"nested": "object"}}, pid),
        {"name": "book_appointment", "call": {"call_id": "junk-8",
         "metadata": {"practice_id": "not-a-uuid"}}, "args": {}},
    ]

    for payload in payloads:
        r = await client.post("/webhooks/retell", json=payload)
        assert r.status_code < 500, (
            f"{r.status_code} on {str(payload)[:120]} — the agent goes quiet here"
        )

    # And the table the injection string named is still there.
    assert (await db_session.execute(
        select(func.count()).select_from(Call)
    )).scalar_one() >= 0


# ── One clinic hammering the tool endpoint ───────────────────────────────────
async def test_a_burst_from_one_call_does_not_wedge_the_endpoint(client, db_session):
    """An agent that loses its place can call a tool repeatedly. Thirty in a row
    must all answer — a caller is on the line for every one of them."""
    practice, _ = await seed_practice(
        db_session, name="Burst Dental", clerk_org_id="org_burst", clerk_user_id="u_burst"
    )
    payloads = [
        _tool("check_availability", f"burst-{i}",
              {"procedure": "cleaning", "preferred_date": "2099-07-01"}, str(practice.id))
        for i in range(30)
    ]
    results = await asyncio.gather(
        *(client.post("/webhooks/retell", json=p) for p in payloads)
    )
    bad = [r.status_code for r in results if r.status_code >= 500]
    assert not bad, f"{len(bad)} of 30 concurrent tool calls failed: {bad[:5]}"


# ── The tenant boundary under concurrency ────────────────────────────────────
async def test_one_clinic_cannot_read_another_while_both_are_busy(client, db_session):
    """Row-level security is the whole tenant boundary. It is enforced per
    session, so the interesting question is not whether it holds when one query
    runs, but whether it holds when many run at once against the same pool."""
    a, _ = await seed_practice(
        db_session, name="Alpha", clerk_org_id="org_alpha", clerk_user_id="u_alpha"
    )
    b, _ = await seed_practice(
        db_session, name="Beta", clerk_org_id="org_beta", clerk_user_id="u_beta"
    )

    async def work(p, tag: str) -> None:
        for i in range(10):
            await client.post("/webhooks/retell", json=_tool(
                "create_callback_request", f"{tag}-{i}",
                {"patient_first_name": tag, "patient_phone": f"+1555300{i:04d}",
                 "reason": tag},
                str(p.id),
            ))

    await asyncio.gather(work(a, "alpha"), work(b, "beta"))

    for practice, tag in ((a, "alpha"), (b, "beta")):
        rows = (await db_session.execute(
            select(Call).where(Call.practice_id == practice.id)
        )).scalars().all()
        assert rows, f"{practice.name} recorded nothing"
        assert all(r.retell_call_id.startswith(tag) for r in rows), (
            f"{practice.name} is holding another clinic's calls"
        )


# ── A call that was never announced ──────────────────────────────────────────
async def test_a_tool_call_for_an_unknown_call_is_refused_not_guessed(client, db_session):
    """No metadata, no prior call_started, and more than one practice exists.
    Guessing here is how one clinic's patient ends up in another's records."""
    for i in range(3):
        await seed_practice(
            db_session, name=f"Ambig {i}",
            clerk_org_id=f"org_ambig_{i}", clerk_user_id=f"u_ambig_{i}",
        )

    r = await client.post("/webhooks/retell", json={
        "name": "book_appointment",
        "call": {"call_id": f"orphan-{uuid.uuid4().hex[:8]}"},
        "args": {"patient_first_name": "Nobody", "preferred_date": "2099-08-01",
                 "preferred_time": "10:00", "procedure": "cleaning"},
    })
    assert r.status_code < 500
    assert not r.json().get("booked"), "booked a patient into a guessed clinic"


async def test_a_malformed_call_object_is_not_a_retry_storm(client):
    """Retell retries a 5xx. A "call" that arrived as a string raised
    AttributeError out of call_started, so one malformed delivery became an
    endless retry — and the call still had no row at the end of it."""
    r = await client.post("/webhooks/retell", json={
        "event": "call_started", "call": "not-an-object",
    })
    assert r.status_code < 500, "a malformed payload must not be retried forever"

    for shape in ("a string", 12345, ["a", "list"], True):
        r = await client.post("/webhooks/retell",
                              json={"event": "call_ended", "call": shape})
        assert r.status_code < 500, f"call={shape!r} produced {r.status_code}"
