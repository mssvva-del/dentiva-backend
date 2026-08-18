"""One clinic, one patient, start to finish — through the real application.

Every other test in this suite proves one thing in isolation. This one walks the
path a real practice actually takes, in order, and checks that each step leaves
the next one possible:

    signs up  →  we approve it  →  onboarding  →  goes live
              →  a patient phones  →  books  →  moves it  →  cancels
              →  the clinic sees all of it on its own screens

It exists because the failures that hurt are not inside a step. They are between
two steps that each pass their own test: a number never provisioned because the
status was wrong, a booking the dashboard cannot count, a time stored one way and
spoken another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.booking import Booking
from app.models.dentiva_staff import DentivaStaff
from app.models.practice import Practice
from app.models.user import User

_ORG = "org_e2e_clinic"
_OWNER = "user_e2e_owner"
_ADMIN = "sa_e2e"
_PATIENT_PHONE = "+16205557788"
_CLINIC_NUMBER = "+15551239876"


def _clinic():
    return {"X-Dev-Clerk-User-Id": _OWNER, "X-Dev-Clerk-Org-Id": _ORG}


def _admin():
    return {"X-Dev-Clerk-User-Id": _ADMIN}


async def _seed_admin(db_session):
    user = User(
        id=uuid.uuid4(), clerk_user_id=_ADMIN, practice_id=None,
        email="ops@dentovox.com", role="staff", is_internal=True,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=user.id, role="super_admin"))
    await db_session.commit()


async def test_a_clinic_signs_up_and_a_patient_gets_an_appointment(
    client, db_session, monkeypatch
):
    await _seed_admin(db_session)

    # ── 1. The clinic appears. First request creates its practice row. ───────
    me = await client.get("/api/practice/me", headers=_clinic())
    assert me.status_code == 200, me.text
    practice_id = uuid.UUID(me.json()["id"])

    listed = await client.get("/api/admin/clinics", headers=_admin())
    assert listed.status_code == 200, listed.text
    assert str(practice_id) in [c["id"] for c in listed.json()], (
        "a clinic that signed up is invisible to the people who must approve it"
    )

    # ── 2. We approve it for a pilot. Without this it cannot have a number. ──
    approved = await client.patch(
        f"/api/admin/clinics/{practice_id}/subscription",
        headers=_admin(), json={"status": "pilot"},
    )
    assert approved.status_code == 200, approved.text

    db_session.expire_all()
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    assert practice.status == "pilot", "approval did not reach the practice itself"

    # ── 3. Onboarding, in the order the wizard walks it. ─────────────────────
    steps = [
        ("/api/onboarding/clinic", {
            "name": "Riverside Dental", "timezone": "America/New_York",
            "address": "1 Main St",
        }),
        # Every day must be present; a closed day is an explicit null rather
        # than an omission, so "we forgot Saturday" and "we are shut on
        # Saturday" cannot look the same to the agent.
        ("/api/onboarding/hours", {"business_hours": {
            **{day: {"open": "09:00", "close": "17:00"}
               for day in ("mon", "tue", "wed", "thu", "fri")},
            "sat": None, "sun": None,
        }}),
        # forward: the clinic keeps its own line and sends unanswered calls to
        # us. transfer_number is where an emergency handoff lands — left NULL it
        # breaks the emergency path silently, which is why the step collects it.
        ("/api/onboarding/phone", {
            "mode": "forward", "forward_number": _CLINIC_NUMBER,
            "transfer_number": _CLINIC_NUMBER,
        }),
        ("/api/onboarding/pms", {"pms_system": "none"}),
        ("/api/onboarding/agent", {
            "agent_name": "Alex", "languages": ["en"], "greeting": None,
        }),
    ]
    for path, payload in steps:
        r = await client.put(path, headers=_clinic(), json=payload)
        assert r.status_code == 200, f"{path} → {r.status_code} {r.text}"

    # The BAA is the hard gate: no signature, no go-live.
    blocked = await client.post("/api/onboarding/complete", headers=_clinic())
    assert blocked.status_code == 403, (
        f"a practice went live without a signed BAA: {blocked.status_code}"
    )

    signed = await client.post("/api/onboarding/baa/accept", headers=_clinic(), json={
        "signer_name": "Dr. Rivers", "signer_title": "Owner", "accepted": True,
    })
    assert signed.status_code == 200, signed.text

    live = await client.post("/api/onboarding/complete", headers=_clinic())
    assert live.status_code == 200, live.text
    assert live.json()["complete"] is True

    # ── 4. A patient phones. The number they dialled is the clinic's. ───────
    db_session.expire_all()
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    practice.ai_phone_number = "+15550001111"
    await db_session.commit()

    async def _call_started(call_id):
        return await client.post("/webhooks/retell", json={
            "event": "call_started", "call_id": call_id,
            "call": {"from_number": _PATIENT_PHONE, "to_number": "+15550001111",
                     "start_timestamp": int(datetime.now(UTC).timestamp() * 1000)},
        })

    async def _tool(call_id, name, args):
        r = await client.post("/webhooks/retell", json={
            "event": "function_call", "call_id": call_id,
            "function_name": name, "args": args,
        })
        assert r.status_code == 200, r.text
        return r.json()

    assert (await _call_started("e2e-1")).status_code == 200

    offered = await _tool("e2e-1", "check_availability", {
        "procedure": "cleaning", "preferred_date": "2099-11-10",
    })
    assert offered["available_slots"], "the agent had nothing to offer a caller"
    slot = offered["available_slots"][0]

    booked = await _tool("e2e-1", "book_appointment", {
        "patient_first_name": "Nina", "patient_last_name": "Reyes",
        "patient_phone": _PATIENT_PHONE, "procedure": "cleaning",
        "preferred_date": slot["date"], "preferred_time_window": "morning",
        "preferred_time": slot["time"],
    })
    assert booked["booked"] is True, booked
    # The time SPOKEN is the time STORED — read back in the clinic's own clock.
    assert booked["appointment"]["time"] == slot["time"]

    # ── 5. The same caller moves it, then cancels it. ───────────────────────
    moved = await _tool("e2e-1", "reschedule_appointment", {
        "patient_phone": _PATIENT_PHONE, "new_date": "2099-11-17",
    })
    assert moved["rescheduled"] is True, moved

    cancelled = await _tool("e2e-1", "cancel_appointment", {
        "patient_phone": _PATIENT_PHONE, "reason": "feeling better",
    })
    assert cancelled["cancelled"] is True, cancelled
    # The sentence names the time it was moved to, in the clinic's own clock —
    # this is the read-back that used to be formatted straight off a UTC column
    # and told the patient an hour they never agreed to.
    assert moved["appointment"]["time"] in cancelled["message"], cancelled["message"]
    assert moved["appointment"]["date"] in cancelled["message"], cancelled["message"]

    db_session.expire_all()
    rows = (await db_session.execute(
        select(Booking).where(Booking.practice_id == practice_id)
    )).scalars().all()
    assert len(rows) == 1, "moving an appointment created a second one"
    assert rows[0].status == "cancelled"

    # ── 6. The clinic can see all of it on its own screens. ────────────────
    for path in (
        "/api/dashboard/today", "/api/dashboard/briefing", "/api/dashboard/weekly",
        "/api/dashboard/conversion", "/api/dashboard/roi",
        "/api/calls/search", "/api/bookings", "/api/patients/search",
    ):
        if path.endswith("search"):
            r = await client.post(path, headers=_clinic(), json={})
        else:
            r = await client.get(path, headers=_clinic())
        assert r.status_code == 200, f"{path} → {r.status_code} {r.text}"

    calls = (await client.post("/api/calls/search", headers=_clinic(), json={})).json()
    assert calls["total"] >= 1, "the call the patient made is not on the clinic's screen"

    patients = (await client.post(
        "/api/patients/search", headers=_clinic(), json={"search": "Reyes"}
    )).json()
    assert patients["total"] >= 1, "the patient who booked is not in the roster"

    # ── 7. And WE can see the clinic. ───────────────────────────────────────
    detail = await client.get(f"/api/admin/clinics/{practice_id}", headers=_admin())
    assert detail.status_code == 200, detail.text
    assert detail.json()["call_count"] >= 1
    assert monkeypatch is not None
