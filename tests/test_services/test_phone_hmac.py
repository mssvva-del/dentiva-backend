"""Deterministic phone hash + indexed patient lookup (perf: no scan/decrypt)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db import set_tenant
from app.models.patient import Patient
from app.utils.crypto import normalize_phone, phone_hmac
from app.webhooks.retell import _find_patient_by_phone, _upsert_patient
from tests.conftest import seed_practice


# ── pure hash ────────────────────────────────────────────────────────────────
def test_normalize_phone_variants_collapse():
    # +1, leading 1, and formatted all normalize to the same 10 digits.
    assert normalize_phone("+15551234567") == "5551234567"
    assert normalize_phone("15551234567") == "5551234567"
    assert normalize_phone("(555) 123-4567") == "5551234567"
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_phone_hmac_deterministic_and_format_insensitive():
    a = phone_hmac("+1 (555) 123-4567")
    b = phone_hmac("5551234567")
    assert a == b and a is not None
    assert phone_hmac("5559999999") != a         # different number → different hash
    assert phone_hmac(None) is None
    assert len(a) == 64                            # sha256 hex


# ── indexed lookup ──────────────────────────────────────────────────────────
async def test_new_patient_gets_phone_hmac_via_listener(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ph1", clerk_org_id="o_ph1", clerk_user_id="u_ph1"
    )
    await set_tenant(db_session, practice.id)
    p = Patient(
        id=uuid.uuid4(), practice_id=practice.id,
        pms_external_id="PH-1", first_name="A", last_name="B", phone="+15551230000",
    )
    db_session.add(p)
    await db_session.commit()
    assert p.phone_hmac == phone_hmac("5551230000")


async def test_upsert_matches_existing_by_indexed_hash(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ph2", clerk_org_id="o_ph2", clerk_user_id="u_ph2"
    )
    await set_tenant(db_session, practice.id)
    first = await _upsert_patient(db_session, practice.id, "Jane", "Doe", "+15557778888")
    await db_session.commit()
    # Same number, different format → same patient (indexed hash match, no new row).
    again = await _upsert_patient(db_session, practice.id, "Jane", "Doe", "(555) 777-8888")
    assert again.id == first.id
    n = len((await db_session.execute(
        select(Patient).where(Patient.practice_id == practice.id)
    )).scalars().all())
    assert n == 1  # no duplicate created


async def test_find_patient_by_phone_indexed(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ph3", clerk_org_id="o_ph3", clerk_user_id="u_ph3"
    )
    await set_tenant(db_session, practice.id)
    made = await _upsert_patient(db_session, practice.id, "Bob", "K", "+15551112222")
    await db_session.commit()
    found = await _find_patient_by_phone(db_session, practice.id, "555-111-2222")
    assert found is not None and found.id == made.id
    assert await _find_patient_by_phone(db_session, practice.id, "5550000000") is None


async def test_phone_update_rehashes_via_listener(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ph4", clerk_org_id="o_ph4", clerk_user_id="u_ph4"
    )
    await set_tenant(db_session, practice.id)
    p = await _upsert_patient(db_session, practice.id, "Amy", "L", "+15551110000")
    await db_session.commit()
    # Change the phone → before_update listener must re-hash it.
    p.phone = "+15552220000"
    await db_session.commit()
    assert p.phone_hmac == phone_hmac("5552220000")
    assert await _find_patient_by_phone(db_session, practice.id, "5552220000") is p
    assert await _find_patient_by_phone(db_session, practice.id, "5551110000") is None


async def test_null_phone_patient_not_matched(db_session):
    practice, _ = await seed_practice(
        db_session, name="Ph5", clerk_org_id="o_ph5", clerk_user_id="u_ph5"
    )
    await set_tenant(db_session, practice.id)
    p = Patient(id=uuid.uuid4(), practice_id=practice.id, pms_external_id="PH-N",
                first_name="No", last_name="Phone", phone=None)
    db_session.add(p)
    await db_session.commit()
    assert p.phone_hmac is None
    assert await _find_patient_by_phone(db_session, practice.id, None) is None


async def test_cross_practice_isolation(db_session):
    pa, _ = await seed_practice(db_session, name="PhA", clerk_org_id="o_pha", clerk_user_id="u_pha")
    pb, _ = await seed_practice(db_session, name="PhB", clerk_org_id="o_phb", clerk_user_id="u_phb")
    await set_tenant(db_session, pa.id)
    a = await _upsert_patient(db_session, pa.id, "Sam", "A", "+15553334444")
    await set_tenant(db_session, pb.id)
    b = await _upsert_patient(db_session, pb.id, "Sam", "B", "+15553334444")  # same number
    await db_session.commit()
    # Same phone, different practices → each resolves to its own patient.
    await set_tenant(db_session, pa.id)
    assert (await _find_patient_by_phone(db_session, pa.id, "5553334444")).id == a.id
    await set_tenant(db_session, pb.id)
    assert (await _find_patient_by_phone(db_session, pb.id, "5553334444")).id == b.id


async def test_a_shared_phone_asks_rather_than_picking_the_oldest(db_session):
    """This used to return the oldest record "deterministically". Deterministic
    it was — deterministically the mother, whoever was actually calling. The
    family's first-registered patient absorbed every other member's calls, and
    since they all pass the caller-ID check on that shared line, each of them
    could act on her appointments believing they were their own.

    Stable and wrong is still wrong. With nothing to tell two people apart, the
    answer is a question, not a guess."""
    from app.webhooks.retell import AMBIGUOUS

    practice, _ = await seed_practice(
        db_session, name="Ph6", clerk_org_id="o_ph6", clerk_user_id="u_ph6"
    )
    await set_tenant(db_session, practice.id)
    mother = Patient(id=uuid.uuid4(), practice_id=practice.id, pms_external_id="PH-C1",
                     first_name="Mom", last_name="X", phone="+15556667777",
                     date_of_birth="1979-02-11")
    db_session.add(mother)
    await db_session.commit()
    child = Patient(id=uuid.uuid4(), practice_id=practice.id, pms_external_id="PH-C2",
                    first_name="Kid", last_name="X", phone="+15556667777",
                    date_of_birth="2011-06-30")
    db_session.add(child)
    await db_session.commit()

    for _ in range(3):  # stable, as before — just no longer a guess
        assert await _find_patient_by_phone(
            db_session, practice.id, "5556667777"
        ) is AMBIGUOUS

    # And each detail the caller can offer resolves it.
    by_dob = await _find_patient_by_phone(
        db_session, practice.id, "5556667777", dob="2011-06-30"
    )
    assert by_dob.id == child.id
    by_name = await _find_patient_by_phone(
        db_session, practice.id, "5556667777", first_name="Mom"
    )
    assert by_name.id == mother.id
