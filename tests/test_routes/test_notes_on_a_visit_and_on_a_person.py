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

from sqlalchemy import select

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


async def test_the_front_desk_can_correct_what_the_call_got_wrong(
    client, db_session
):
    """Every field the agent fills is a field that can be wrong: a name heard
    over a bad line, a number written down before the caller corrected it, a
    birthday given as a month and a day."""
    _, patient, _ = await _seed(db_session, "g")
    h = _auth("org_ng", "user_ng")

    r = await client.patch(
        f"/api/patients/{patient.id}",
        json={"first_name": "Maria", "last_name": "Lopez-Ruiz",
              "phone": "+16175550142", "date_of_birth": "1979-03-02",
              "preferred_language": "es"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_name"] == "Maria"
    assert body["last_name"] == "Lopez-Ruiz"
    assert body["phone"] == "+16175550142"
    assert body["date_of_birth"] == "1979-03-02"
    assert body["preferred_language"] == "es"


async def test_a_corrected_number_is_findable_by_the_next_caller(
    client, db_session
):
    """The searchable copy of the phone has to move with it, or the patient is
    invisible to the lookup that runs on every incoming call."""
    from app.utils.crypto import phone_hmac

    practice, patient, _ = await _seed(db_session, "h")
    practice_id = practice.id
    await client.patch(
        f"/api/patients/{patient.id}",
        json={"phone": "+16175550143"},
        headers=_auth("org_nh", "user_nh"),
    )
    pid = patient.id  # read before expiring: an expired instance cannot load here
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    fresh = (await db_session.execute(
        select(Patient).where(Patient.id == pid)
    )).scalar_one()
    assert fresh.phone_hmac == phone_hmac("+16175550143")


async def test_a_birthday_that_is_not_a_date_is_refused(client, db_session):
    """A junk birthday silences the lookup that tells two people on one number
    apart — worse than leaving it empty."""
    _, patient, _ = await _seed(db_session, "i")
    r = await client.patch(
        f"/api/patients/{patient.id}",
        json={"date_of_birth": "1979-02-30"},   # a date that never happened
        headers=_auth("org_ni", "user_ni"),
    )
    assert r.status_code == 400, r.text
    assert "1968-04-09" in r.text  # the message shows the shape it wants


async def test_a_number_that_is_not_a_number_is_refused(client, db_session):
    _, patient, _ = await _seed(db_session, "j")
    r = await client.patch(
        f"/api/patients/{patient.id}",
        json={"phone": "555-1234"},   # what a speech model heard, not a number
        headers=_auth("org_nj", "user_nj"),
    )
    assert r.status_code == 400, r.text
