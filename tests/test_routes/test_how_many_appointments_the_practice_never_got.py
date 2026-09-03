"""The number a clinic should see before their patients do.

A live practice found appointments missing from their own calendar before we
noticed. The booking page had been saying it all along — one appointment at a
time, to whoever happened to open that one — so nobody read it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db import set_tenant
from app.models.booking import Booking
from app.models.patient import Patient
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def _clinic(db_session, tag: str, **practice_kw):
    practice, _ = await seed_practice(
        db_session, name=f"Step {tag}",
        clerk_org_id=f"org_o{tag}", clerk_user_id=f"user_o{tag}", **practice_kw,
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=f"o-{tag}",
        first_name="Ruth", last_name="Delaney", phone="+15551239000",
    )
    db_session.add(patient)
    await db_session.flush()
    return practice, patient


def _booking(practice, patient, *, days: int, pms_id: str | None, status="confirmed"):
    return Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=days),
        duration_minutes=45, procedure_type="cleaning", status=status,
        pms_external_id=pms_id,
    )


async def test_it_counts_only_what_the_practice_is_missing(
    client, db_session, monkeypatch
):
    from app.routes import bookings as route

    practice, patient = await _clinic(db_session, "a")
    monkeypatch.setattr(route, "pms_is_connected", lambda _p: True)
    db_session.add_all([
        _booking(practice, patient, days=3, pms_id=None),        # missing
        _booking(practice, patient, days=4, pms_id=None),        # missing
        _booking(practice, patient, days=5, pms_id="1677100"),   # they have it
        _booking(practice, patient, days=-2, pms_id=None),       # already past
        _booking(practice, patient, days=6, pms_id=None, status="cancelled"),
    ])
    await db_session.commit()

    r = await client.get("/api/bookings/out-of-step", headers=_auth("org_oa", "user_oa"))
    assert r.status_code == 200, r.text
    assert r.json() == {"count": 2, "pms_connected": True}


async def test_a_clinic_without_practice_software_is_never_warned(
    client, db_session, monkeypatch
):
    """Our book IS their calendar. A permanent warning there would be a lie."""
    from app.routes import bookings as route

    practice, patient = await _clinic(db_session, "b")
    monkeypatch.setattr(route, "pms_is_connected", lambda _p: False)
    db_session.add(_booking(practice, patient, days=3, pms_id=None))
    await db_session.commit()

    r = await client.get("/api/bookings/out-of-step", headers=_auth("org_ob", "user_ob"))
    assert r.json() == {"count": 0, "pms_connected": False}


async def test_one_practice_cannot_count_anothers(client, db_session, monkeypatch):
    from app.routes import bookings as route

    practice, patient = await _clinic(db_session, "c")
    monkeypatch.setattr(route, "pms_is_connected", lambda _p: True)
    db_session.add(_booking(practice, patient, days=3, pms_id=None))
    await seed_practice(
        db_session, name="Someone Else", clerk_org_id="org_ox", clerk_user_id="user_ox"
    )
    await db_session.commit()

    r = await client.get("/api/bookings/out-of-step", headers=_auth("org_ox", "user_ox"))
    assert r.json()["count"] == 0
