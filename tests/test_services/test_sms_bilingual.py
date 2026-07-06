"""Bilingual voice loop — booking/cancellation SMS localize to the call language."""

from __future__ import annotations

from app.db import set_tenant
from app.services.sms import build_cancellation_body, build_confirmation_body
from app.webhooks.retell import _upsert_patient
from tests.conftest import seed_practice


def test_confirmation_english_default():
    body = build_confirmation_body(
        practice_name="Bright Smiles", first_name="Maria",
        date="2026-07-01", time="10:00", provider="Dr. Smith",
    )
    assert "is confirmed for" in body and "Hi Maria" in body


def test_confirmation_spanish():
    body = build_confirmation_body(
        practice_name="Bright Smiles", first_name="María",
        date="2026-07-01", time="10:00", provider="Dr. Smith", language="es",
    )
    assert "está confirmada" in body and "Hola María" in body and "con Dr. Smith" in body


def test_confirmation_language_tag_variants():
    # es-US / ES / español-ish all map to Spanish; unknown → English.
    assert "está confirmada" in build_confirmation_body(
        practice_name="X", first_name=None, date="d", time="t",
        provider=None, language="es-US",
    )
    assert "is confirmed" in build_confirmation_body(
        practice_name="X", first_name=None, date="d", time="t",
        provider=None, language="fr",
    )


def test_cancellation_spanish():
    body = build_cancellation_body(
        practice_name="Bright Smiles", first_name=None,
        date="2026-07-01", time="10:00", language="es",
    )
    assert "ha sido cancelada" in body and body.startswith("Hola, ")


async def test_new_patient_stores_spanish_preference(db_session):
    practice, _ = await seed_practice(
        db_session, name="ES Loop", clerk_org_id="org_esl", clerk_user_id="u_esl"
    )
    await set_tenant(db_session, practice.id)
    p = await _upsert_patient(
        db_session, practice.id, "Juan", "Pérez", "+15551230000", language="es-US"
    )
    # New Spanish-call patient → preferred_language 'es' → bilingual SMS loop closes.
    assert p.preferred_language == "es"

    # English/None caller → default 'en'.
    p2 = await _upsert_patient(
        db_session, practice.id, "John", "Doe", "+15551239999", language=None
    )
    assert p2.preferred_language == "en"
