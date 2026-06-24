"""Reactivation Engine schema (block 1): defaults + tenant isolation.

Verifies the new schema is sound and — critically — that the three reactivation
tables are RLS-isolated like the rest of the PHI surface. These tables hold a
clinic's dormant-patient list, so a tenant leak here is exactly the
company-ending bug the RLS backstop exists to prevent.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

import app.db as app_db
from app.db import set_tenant
from app.models.patient import Patient
from app.models.reactivation import (
    ReactivationCampaign,
    ReactivationTarget,
    ReactivationTouch,
)
from tests.conftest import seed_practice


async def _seed_patient(db_session, practice, ext_id):
    await set_tenant(db_session, practice.id)
    p = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id=ext_id,
        first_name="Lapsed", last_name="Patient", phone="+15550000000",
    )
    db_session.add(p)
    await db_session.flush()
    return p


async def _seed_campaign_chain(db_session, practice, patient, name):
    """Campaign → target → touch under one practice. db_session is the superuser
    seed session (bypasses RLS), so it can set up any tenant's rows."""
    await set_tenant(db_session, practice.id)
    camp = ReactivationCampaign(
        id=uuid.uuid4(), practice_id=practice.id, name=name, segment="lapsed",
    )
    db_session.add(camp)
    await db_session.flush()
    tgt = ReactivationTarget(
        id=uuid.uuid4(), practice_id=practice.id, campaign_id=camp.id,
        patient_id=patient.id, segment="lapsed",
    )
    db_session.add(tgt)
    await db_session.flush()
    touch = ReactivationTouch(
        id=uuid.uuid4(), practice_id=practice.id, target_id=tgt.id, channel="sms",
    )
    db_session.add(touch)
    await db_session.commit()
    return camp, tgt, touch


async def test_preferred_language_defaults_to_en(db_session):
    practice, _ = await seed_practice(
        db_session, name="Lang", clerk_org_id="org_lang", clerk_user_id="u_lang"
    )
    p = await _seed_patient(db_session, practice, "lang-1")
    await db_session.commit()
    await db_session.refresh(p)
    assert p.preferred_language == "en"


async def test_reactivation_defaults(db_session):
    practice, _ = await seed_practice(
        db_session, name="Defaults", clerk_org_id="org_def", clerk_user_id="u_def"
    )
    pat = await _seed_patient(db_session, practice, "def-1")
    camp, tgt, touch = await _seed_campaign_chain(db_session, practice, pat, "C1")
    await db_session.refresh(camp)
    await db_session.refresh(tgt)
    await db_session.refresh(touch)
    assert camp.status == "draft"
    assert tgt.status == "pending" and str(tgt.value_score) == "0.00"
    assert tgt.touches_count == 0
    assert touch.status == "queued" and touch.language == "en"


async def test_reactivation_tables_rls_isolated(db_session):
    """Bound to tenant A, a raw unfiltered SELECT on each reactivation table must
    return only A's rows — never B's."""
    a, _ = await seed_practice(
        db_session, name="RA", clerk_org_id="org_ra", clerk_user_id="u_ra"
    )
    b, _ = await seed_practice(
        db_session, name="RB", clerk_org_id="org_rb", clerk_user_id="u_rb"
    )
    a_pat = await _seed_patient(db_session, a, "ra-1")
    b_pat = await _seed_patient(db_session, b, "rb-1")
    await db_session.commit()
    await _seed_campaign_chain(db_session, a, a_pat, "A-camp")
    await _seed_campaign_chain(db_session, b, b_pat, "B-camp")

    async with app_db.async_session_factory() as session:  # dentiva_app = RLS on
        await set_tenant(session, a.id)
        for tbl in (
            "reactivation_campaigns", "reactivation_targets", "reactivation_touches"
        ):
            pids = {
                str(r[0])
                for r in (await session.execute(text(f"SELECT practice_id FROM {tbl}"))).all()  # noqa: S608
            }
            assert str(a.id) in pids, f"{tbl}: tenant A's own row missing"
            assert str(b.id) not in pids, f"{tbl}: RLS leaked tenant B!"
