"""What the caller said, and what the practice learned afterwards.

Callers tell the agent things no column holds: which tooth, who is driving them,
that the last cleaning hurt. All of it lived inside a transcript nobody opens
before the patient walks in — the front desk saw "Cleaning, 9:00" and nothing
else.

Two notes with two lifetimes. The visit note belongs to one appointment and the
agent writes it while the caller is talking. The person note outlives every
appointment and the front desk types it after they meet them.
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


async def _seed(db_session, tag: str):
    practice, _ = await seed_practice(
        db_session, name=f"Notes Dental {tag}",
        clerk_org_id=f"org_n{tag}", clerk_user_id=f"user_n{tag}",
    )
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=f"n-{tag}",
        first_name="Maria", last_name="Lopez", phone="+15557770000",
    )
    db_session.add(patient)
    await db_session.flush()
    booking = Booking(
        id=uuid.uuid4(), practice_id=practice.id, patient_id=patient.id,
        appointment_at=datetime.now(UTC) + timedelta(days=3),
        procedure_type="cleaning", status="confirmed",
        notes="Upper left tooth hurts on cold. Her daughter drives her.",
    )
    db_session.add(booking)
    await db_session.commit()
    return practice, patient, booking


async def test_the_visit_note_reaches_every_screen_the_front_desk_opens(
    client, db_session
):
    """The list, the card and the export all read from the same row — a note
    visible in only one of them is a note nobody finds."""
    _, _, booking = await _seed(db_session, "a")
    h = _auth("org_na", "user_na")

    detail = await client.get(f"/api/bookings/{booking.id}", headers=h)
    assert detail.status_code == 200, detail.text
    assert "Upper left tooth" in detail.json()["notes"]

    listing = await client.get("/api/bookings", headers=h)
    row = next(b for b in listing.json()["bookings"] if b["id"] == str(booking.id))
    assert "Upper left tooth" in row["notes"]


async def test_the_front_desk_can_correct_a_visit_note(client, db_session):
    """The caller says one thing on the phone and something else at the desk."""
    _, _, booking = await _seed(db_session, "b")
    r = await client.patch(
        f"/api/bookings/{booking.id}",
        json={"notes": "It is the lower left, not the upper. Cold only."},
        headers=_auth("org_nb", "user_nb"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"].startswith("It is the lower left")
    # Amending a note is not moving an appointment.
    assert r.json()["appointment_at"] is not None


async def test_the_person_note_outlives_the_appointment(client, db_session):
    """Allergies and "always brings her daughter" belong to the patient, not to
    one visit — otherwise they are retyped, or lost, every time."""
    _, patient, _ = await _seed(db_session, "c")
    h = _auth("org_nc", "user_nc")

    r = await client.patch(
        f"/api/patients/{patient.id}",
        json={"notes": "Latex allergy. Pays by card only."},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "Latex allergy. Pays by card only."

    # And it is still there on the next visit's screen.
    again = await client.get(f"/api/patients/{patient.id}", headers=h)
    assert again.json()["notes"] == "Latex allergy. Pays by card only."


async def test_a_note_is_stored_encrypted(client, db_session):
    """It is the patient's own words about their health, which is why the name
    beside it is encrypted too. A database dump must not read like a chart."""
    from sqlalchemy import text

    _, patient, booking = await _seed(db_session, "d")
    await client.patch(
        f"/api/patients/{patient.id}",
        json={"notes": "Latex allergy."},
        headers=_auth("org_nd", "user_nd"),
    )
    await set_tenant(db_session, booking.practice_id)
    raw = (await db_session.execute(
        text("select notes from bookings where id = :i"), {"i": str(booking.id)}
    )).scalar_one()
    assert b"Upper left tooth" not in bytes(raw)


async def test_one_practice_cannot_read_anothers_notes(client, db_session):
    _, patient, booking = await _seed(db_session, "e")
    other, _ = await seed_practice(
        db_session, name="Someone Else", clerk_org_id="org_nx", clerk_user_id="user_nx"
    )
    await db_session.commit()
    h = _auth("org_nx", "user_nx")
    assert (await client.get(f"/api/bookings/{booking.id}", headers=h)).status_code == 404
    assert (await client.patch(
        f"/api/patients/{patient.id}", json={"notes": "x"}, headers=h
    )).status_code == 404


async def test_an_empty_patient_edit_is_refused(client, db_session):
    _, patient, _ = await _seed(db_session, "f")
    r = await client.patch(
        f"/api/patients/{patient.id}", json={}, headers=_auth("org_nf", "user_nf")
    )
    assert r.status_code == 422
