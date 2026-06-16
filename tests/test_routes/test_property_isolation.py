"""Property-based tenant isolation (audit Risk 5).

The single hardcoded A/B test in test_multitenant.py proves RLS works for ONE
shape of data. This fuzzes the isolation boundary: random tenant counts, random
row counts per tenant, random phones — and asserts the SAME invariant on every
generated example:

  bound to tenant T, a raw unfiltered SELECT on each PHI table returns EXACTLY
  T's rows — never another tenant's (no leak) and never missing one (completeness).

Deterministic (seeded RNG) so a CI failure reproduces exactly. We drive the
example loop manually rather than via Hypothesis: Hypothesis's @given fights
pytest-asyncio (function-scoped-fixture reuse + shared event loop), which would
make this flaky — the property here is the randomized input + invariant, which a
seeded loop delivers reliably.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

import app.db as app_db
from app.db import set_tenant
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from tests.conftest import seed_practice

PHI_TABLES = ("patients", "calls", "bookings")


def _phone(rng: random.Random) -> str:
    return "+1" + "".join(str(rng.randint(0, 9)) for _ in range(10))


async def _seed_tenant_rows(
    db_session, practice, rng: random.Random, n_patients: int
) -> dict[str, set[str]]:
    """Seed a random set of PHI rows for one tenant; return the ids we created,
    keyed by table — the exact set RLS must return when bound to this tenant."""
    # db_session is the superuser seed session (bypasses RLS); set_tenant anyway
    # so the rows carry the right tenant and we mirror the real write path.
    await set_tenant(db_session, practice.id)
    ids: dict[str, set[str]] = {t: set() for t in PHI_TABLES}

    for i in range(n_patients):
        patient = Patient(
            id=uuid.uuid4(),
            practice_id=practice.id,
            pms_external_id=f"{practice.id}-{i}",
            first_name="Pat",
            last_name="Random",
            phone=_phone(rng),
        )
        db_session.add(patient)
        await db_session.flush()
        ids["patients"].add(str(patient.id))

        for _ in range(rng.randint(0, 3)):
            call = Call(
                id=uuid.uuid4(),
                practice_id=practice.id,
                retell_call_id=str(uuid.uuid4()),
                direction="inbound",
                from_number=_phone(rng),
                to_number=_phone(rng),
                patient_id=patient.id,
                started_at=datetime.now(UTC),
                status="completed",
                outcome="info_only",
            )
            db_session.add(call)
            await db_session.flush()
            ids["calls"].add(str(call.id))

        for _ in range(rng.randint(0, 2)):
            booking = Booking(
                id=uuid.uuid4(),
                practice_id=practice.id,
                patient_id=patient.id,
                appointment_at=datetime.now(UTC) + timedelta(days=1),
                duration_minutes=60,
                status="confirmed",
                source="ai_call",
            )
            db_session.add(booking)
            await db_session.flush()
            ids["bookings"].add(str(booking.id))

    await db_session.commit()
    return ids


async def test_rls_isolation_property(client, db_session):
    """Fuzz: across many random multi-tenant layouts, each tenant sees exactly
    its own PHI rows and nothing from any sibling tenant."""
    # Rows accumulate across examples (no schema reset), but practice_ids are
    # unique per example so the per-tenant invariant stays exact — and an example
    # that runs against a fuller table is a STRONGER isolation test, not a weaker
    # one.
    for seed in range(30):
        rng = random.Random(seed)
        n_tenants = rng.randint(2, 4)
        tenants = []
        for t in range(n_tenants):
            practice, _ = await seed_practice(
                db_session,
                name=f"Prop {seed}-{t}",
                clerk_org_id=f"org_prop_{seed}_{t}",
                clerk_user_id=f"user_prop_{seed}_{t}",
            )
            ids = await _seed_tenant_rows(db_session, practice, rng, rng.randint(1, 3))
            tenants.append((practice, ids))

        for practice, ids in tenants:
            # Real app path: dentiva_app role, RLS enforced.
            async with app_db.async_session_factory() as session:
                await set_tenant(session, practice.id)
                for tbl in PHI_TABLES:
                    rows = (
                        await session.execute(text(f"SELECT id, practice_id FROM {tbl}"))  # noqa: S608
                    ).all()
                    seen_pids = {str(r[1]) for r in rows}
                    seen_ids = {str(r[0]) for r in rows}
                    assert seen_pids <= {str(practice.id)}, (
                        f"seed={seed} {tbl}: RLS leaked tenant(s) {seen_pids - {str(practice.id)}}"
                    )
                    assert seen_ids == ids[tbl], (
                        f"seed={seed} {tbl}: visible rows != seeded rows for {practice.id}"
                    )
