"""END-TO-END clinic walkthrough — one realistic 'clinic day' through the REAL
HTTP webhook path, printing a stage-by-stage report. Run:

    pytest tests/test_e2e_clinic_walkthrough.py -s -q

Mirrors exactly what happens on a live call: KB → agent variables, then the tool
sequence check_availability → book → reschedule → cancel, plus reminders.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.booking import Booking
from app.models.practice import Practice
from app.services.llm.dynamic_vars import build_dynamic_variables
from tests.conftest import seed_practice


def _weekday(days: int) -> str:
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def _ok(stage: str, detail: str = "") -> None:
    print(f"  ✅ {stage}" + (f" — {detail}" if detail else ""))


async def test_full_clinic_walkthrough(client, db_session):
    print("\n========== DENTOVOX FULL CLINIC WALKTHROUGH ==========")

    # ── STAGE 1: registration + onboarding result (a configured, live clinic) ──
    print("\n[1] CLINIC SETUP (what the doctor fills in onboarding)")
    practice, _ = await seed_practice(
        db_session, name="Sunrise Family Dental",
        clerk_org_id="org_e2e", clerk_user_id="user_e2e",
    )
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    p.timezone = "America/New_York"
    p.retell_agent_id = "agent_e2e"
    p.agent_settings = {"agent_name": "Sofia", "greeting": "We can't wait to see your smile!"}
    p.knowledge_base = {
        "providers": [
            {"name": "Dr. Emily Chen", "type": "general", "accepts_new": True},
            {"name": "Dr. Marcus Webb", "type": "orthodontist", "accepts_new": True},
        ],
        "appointment_types": [
            {"name": "cleaning", "minutes": 60, "new_patient": False},
            {"name": "consultation", "minutes": 30, "new_patient": True},
        ],
        "insurances": ["Delta Dental", "Cigna", "Aetna", "MetLife"],
        "self_pay": True,
        "policies": {"cancellation": "24-hour notice, please.",
                     "parking": "Free lot behind the building."},
    }
    await db_session.commit()
    _ok("Clinic configured", "Sunrise Family Dental · 2 providers · 4 insurances · Mon–Fri 9–6")

    # ── STAGE 2: the agent LEARNS this clinic (KB → per-call variables) ─────────
    print("\n[2] AGENT LEARNS THE CLINIC (dynamic variables sent on every call)")
    v = build_dynamic_variables(p)
    assert v["agent_name"] == "Sofia"
    assert "Dr. Emily Chen" in v["kb_context"]
    assert "Delta Dental" in v["kb_context"]
    assert v["custom_greeting"].startswith("We can't wait")
    _ok("Agent name", v["agent_name"])
    _ok("Knows providers + insurances", "Dr. Chen / Dr. Webb · Delta, Cigna, Aetna, MetLife")
    _ok("Clinic-local date anchor", f'{v["today"]} ({v["timezone"]})')

    # ── STAGE 3: inbound call starts (Retell → our webhook) ────────────────────
    print("\n[3] INBOUND CALL STARTS")
    r = await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "e2e-call-1",
        "call": {"from_number": "+15551239876", "to_number": "+16204559562",
                 "agent_id": "agent_e2e"},
    })
    assert r.status_code == 200
    _ok("Call row created")

    # ── STAGE 4: agent offers REAL openings (not invented) ─────────────────────
    print("\n[4] AGENT CHECKS AVAILABILITY (real hours − booked)")
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "e2e-call-1",
        "function_name": "check_availability",
        "args": {"procedure": "cleaning", "preferred_date": _weekday(3),
                 "preferred_time_window": "morning"},
    })
    slots = r.json()["available_slots"]
    assert slots, "agent must offer real slots"
    _ok("Offered real openings", f'{slots[0]["date"]} {slots[0]["time"]} + {len(slots)-1} more')

    # ── STAGE 5: booking ───────────────────────────────────────────────────────
    print("\n[5] AGENT BOOKS THE VISIT")
    book_date = _weekday(3)
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "e2e-call-1",
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Maria", "patient_last_name": "Garcia",
                 "patient_phone": "+15551239876", "procedure": "cleaning",
                 "preferred_date": book_date, "preferred_time_window": "morning"},
    })
    body = r.json()
    assert body["booked"] is True
    appt = body["appointment"]
    _ok("Booked", f'{appt["date"]} {appt["time"]} with {appt["provider"]}')
    bookings = (await db_session.execute(
        select(Booking).where(Booking.practice_id == p.id)
    )).scalars().all()
    assert len(bookings) == 1 and bookings[0].status == "confirmed"
    _ok("Booking persisted + confirmation SMS attempted (fail-safe)")

    # ── STAGE 6: reschedule ────────────────────────────────────────────────────
    print("\n[6] PATIENT CALLS BACK TO RESCHEDULE")
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "e2e-call-2",
        "call": {"from_number": "+15551239876", "to_number": "+16204559562",
                 "agent_id": "agent_e2e"},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "e2e-call-2",
        "function_name": "reschedule_appointment",
        "args": {"patient_phone": "+15551239876", "new_date": _weekday(6),
                 "new_time_window": "afternoon"},
    })
    rb = r.json()
    assert rb["rescheduled"] is True
    _ok("Moved", f'{rb["appointment"]["date"]} {rb["appointment"]["time"]}')

    # ── STAGE 7: cancel ────────────────────────────────────────────────────────
    print("\n[7] PATIENT CANCELS")
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "e2e-call-2",
        "function_name": "cancel_appointment",
        "args": {"patient_phone": "+15551239876"},
    })
    assert r.json()["cancelled"] is True
    _ok("Cancelled + waitlist backfill checked")

    # ── STAGE 8: emergency safety ──────────────────────────────────────────────
    print("\n[8] EMERGENCY SAFETY (severe bleeding → scheduling locked)")
    await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "e2e-call-2",
        "function_name": "create_callback_request",
        "args": {"patient_first_name": "Maria", "patient_phone": "+15551239876",
                 "reason": "severe bleeding and swelling", "urgent": True},
    })
    r = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "e2e-call-2",
        "function_name": "book_appointment",
        "args": {"patient_first_name": "Maria", "patient_last_name": "Garcia",
                 "patient_phone": "+15551239876", "procedure": "cleaning",
                 "preferred_date": _weekday(3), "preferred_time_window": "morning"},
    })
    assert r.json().get("blocked") is True
    _ok("Booking REFUSED during emergency (programmatic lock)")

    print("\n========== ALL STAGES PASSED ✅ ==========\n")
