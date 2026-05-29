"""The critical test: practice A must never see practice B's data."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

import app.db as app_db
from app.db import set_tenant
from app.models.call import Call
from app.models.patient import Patient
from tests.conftest import seed_practice


async def _seed_patient_and_call(db_session, practice, ext_id, retell_id):
    # RLS INSERT check requires the tenant setting to match the row's practice_id.
    await set_tenant(db_session, practice.id)
    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice.id,
        pms_external_id=ext_id,
        first_name="Test",
        last_name="Patient",
        phone="+15550000000",
    )
    db_session.add(patient)
    await db_session.flush()
    call = Call(
        id=uuid.uuid4(),
        practice_id=practice.id,
        retell_call_id=retell_id,
        direction="inbound",
        from_number="+15551112222",
        to_number="+15559998888",
        patient_id=patient.id,
        started_at=datetime.now(UTC),
        status="completed",
        outcome="info_only",
    )
    db_session.add(call)
    await db_session.commit()
    return patient, call


async def test_calls_isolated_between_practices(client, db_session):
    a_practice, _ = await seed_practice(
        db_session, name="Practice A", clerk_org_id="org_A2", clerk_user_id="user_A2"
    )
    b_practice, _ = await seed_practice(
        db_session, name="Practice B", clerk_org_id="org_B2", clerk_user_id="user_B2"
    )
    await _seed_patient_and_call(db_session, a_practice, "A-1", "ra1")
    await _seed_patient_and_call(db_session, b_practice, "B-1", "rb1")

    # User A sees only A's call.
    resp = await client.get(
        "/api/calls", headers={"X-Dev-Clerk-User-Id": "user_A2"}
    )
    assert resp.status_code == 200
    calls = resp.json()["calls"]
    assert len(calls) == 1
    assert calls[0]["from_number"] == "+15551112222"
    assert calls[0]["patient_name_redacted"] == "Test P."


async def test_rls_blocks_cross_tenant_patient_read(db_session):
    """Even with NO python WHERE filter, RLS must hide other tenants' rows."""
    a_practice, _ = await seed_practice(
        db_session, name="Practice A3", clerk_org_id="org_A3", clerk_user_id="user_A3"
    )
    b_practice, _ = await seed_practice(
        db_session, name="Practice B3", clerk_org_id="org_B3", clerk_user_id="user_B3"
    )
    await _seed_patient_and_call(db_session, a_practice, "A3-1", "ra3")
    await _seed_patient_and_call(db_session, b_practice, "B3-1", "rb3")

    # Open a fresh session, set tenant to A, then SELECT * with no filter.
    async with app_db.async_session_factory() as session:
        await set_tenant(session, a_practice.id)
        rows = (await session.execute(text("SELECT practice_id FROM patients"))).all()
    returned = {str(r[0]) for r in rows}
    assert str(a_practice.id) in returned
    assert str(b_practice.id) not in returned, "RLS leaked another tenant's rows!"
