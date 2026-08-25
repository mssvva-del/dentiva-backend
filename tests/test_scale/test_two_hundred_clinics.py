"""What breaks between one clinic and two hundred.

Everything here passes trivially at n=1. These are the failures that only exist
once a group practice onboards its whole estate: a routing key that is unique in
theory, an ambiguity resolver that never fires, an isolation guarantee that has
only ever been checked against two tenants.

A DSO does not discover these one at a time and forgive them. It discovers the
first one and stops the rollout.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models.patient import Patient
from app.models.practice import Practice
from tests.conftest import seed_practice

pytestmark = pytest.mark.asyncio

# Enough to be a real estate, small enough to keep the suite honest about time.
FLEET = 60


async def _fleet(db_session, n: int = FLEET, prefix: str = "dso") -> list[Practice]:
    """n practices, each with its own Dentovox number and its own name."""
    made = []
    for i in range(n):
        practice, _ = await seed_practice(
            db_session,
            name=f"{prefix.title()} Dental {i:03d}",
            clerk_org_id=f"org_{prefix}_{i:03d}",
            clerk_user_id=f"u_{prefix}_{i:03d}",
        )
        # Distinct, valid E.164 numbers across three area codes.
        practice.ai_phone_number = f"+1{617 + (i % 3):03d}555{i:04d}"
        practice.knowledge_base = {
            "providers": [{"name": f"Dr. Number {i:03d}"}],
            "insurances": [f"Plan {i:03d}"],
        }
        made.append(practice)
    await db_session.commit()
    return made


def _inbound(to_number: str) -> dict:
    return {"call_inbound": {"agent_id": None, "to_number": to_number}}


# ── Routing ────────────────────────────────────────────────────────────────


async def test_every_clinic_hears_its_own_name(client, db_session):
    """The whole product at scale, in one assertion: sixty numbers, sixty
    clinics, and not one call that greets a caller with somebody else's
    practice."""
    fleet = await _fleet(db_session, prefix="own")

    for practice in fleet:
        r = await client.post("/webhooks/retell/inbound",
                              json=_inbound(practice.ai_phone_number))
        assert r.status_code == 200
        variables = r.json()["call_inbound"]["dynamic_variables"]
        assert variables.get("practice_name") == practice.name, (
            f"{practice.ai_phone_number} answered as {variables.get('practice_name')!r}"
        )


async def test_concurrent_calls_do_not_bleed_into_each_other(client, db_session):
    """Sixty clinics ringing at once. Each response must belong to the number
    that asked — a shared session, a cached settings object or a module-level
    variable would show up here and nowhere else."""
    fleet = await _fleet(db_session, prefix="conc")

    async def ask(practice: Practice) -> tuple[str, str]:
        r = await client.post("/webhooks/retell/inbound",
                              json=_inbound(practice.ai_phone_number))
        return practice.name, r.json()["call_inbound"]["dynamic_variables"].get(
            "practice_name", ""
        )

    results = await asyncio.gather(*(ask(p) for p in fleet))
    wrong = [(expected, got) for expected, got in results if expected != got]
    assert not wrong, f"{len(wrong)} calls answered as the wrong clinic: {wrong[:3]}"


async def test_an_unknown_number_is_refused_not_guessed(client, db_session):
    """With one clinic in the database, falling back to "the only practice" is
    correct. With sixty it is a coin flip that lands on somebody's patients."""
    await _fleet(db_session, prefix="unk")

    r = await client.post("/webhooks/retell/inbound", json=_inbound("+16175559999"))
    assert r.status_code == 200
    variables = r.json()["call_inbound"]["dynamic_variables"]
    assert not variables.get("practice_name"), (
        "an unmatched number was attributed to a real clinic"
    )


async def test_a_duplicated_practice_phone_refuses_rather_than_picks(
    client, db_session
):
    """ai_phone_number carries a UNIQUE constraint. practices.phone_number — the
    clinic's own line, which the resolver also matches on — does not.

    Two rows can therefore hold the same number: a bulk import, a copy-paste
    during a 200-clinic onboarding, a group that lists one central line on every
    location. The resolver takes .first(), so every call on that number reaches
    whichever row sorts first, and the other clinic's patients are recorded
    against a practice they never called.

    Ambiguous must mean REFUSED, exactly as it does for an unmatched number."""
    a, _ = await seed_practice(
        db_session, name="Twin A", clerk_org_id="org_twin_a", clerk_user_id="u_twin_a"
    )
    b, _ = await seed_practice(
        db_session, name="Twin B", clerk_org_id="org_twin_b", clerk_user_id="u_twin_b"
    )
    shared = "+16175550777"
    a.phone_number = shared
    b.phone_number = shared
    await db_session.commit()

    r = await client.post("/webhooks/retell/inbound", json=_inbound(shared))
    assert r.status_code == 200
    name = r.json()["call_inbound"]["dynamic_variables"].get("practice_name")
    assert name not in (a.name, b.name), (
        f"an ambiguous number was attributed to {name!r} — the other clinic's "
        "calls are being recorded against it"
    )


# ── Isolation ──────────────────────────────────────────────────────────────


async def test_row_level_security_holds_across_a_whole_estate(db_session):
    """RLS is usually proven with two tenants. A guarantee that holds for two and
    not for sixty is worth nothing to a group practice."""
    import app.db as app_db
    from app.db import set_tenant

    fleet = await _fleet(db_session, n=12, prefix="rls")
    for practice in fleet:
        db_session.add(Patient(
            id=uuid.uuid4(), practice_id=practice.id,
            pms_external_id=f"ext-{practice.name[-3:]}",
            first_name="Pat", last_name=f"Of{practice.name[-3:]}",
            phone=f"+1978555{practice.name[-3:]}0",
        ))
    await db_session.commit()

    for practice in fleet:
        async with app_db.async_session_factory() as session:
            await set_tenant(session, practice.id)
            rows = (await session.execute(select(Patient))).scalars().all()
            others = [p for p in rows if p.practice_id != practice.id]
            assert not others, (
                f"{practice.name} can see {len(others)} patients belonging to "
                "other clinics"
            )


# ── Onboarding at scale ────────────────────────────────────────────────────


async def test_bulk_onboarding_never_hands_two_clinics_one_number(db_session):
    """The unique constraint is the last line of defence, and it must actually
    be there — routing keys off this column, and two clinics answering one
    number is the failure every other guard in this system exists to prevent."""
    from sqlalchemy.exc import IntegrityError

    a, _ = await seed_practice(
        db_session, name="Dup A", clerk_org_id="org_dup_a", clerk_user_id="u_dup_a"
    )
    b, _ = await seed_practice(
        db_session, name="Dup B", clerk_org_id="org_dup_b", clerk_user_id="u_dup_b"
    )
    a.ai_phone_number = "+16175551234"
    await db_session.commit()

    b.ai_phone_number = "+16175551234"
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_the_autolink_states_its_own_limit(db_session, monkeypatch):
    """Written for one clinic at a time: it links only when exactly one practice
    is waiting and exactly one location is unclaimed. That is right for safety
    and useless for a rollout — with a fleet onboarding together it will never
    fire, and every clinic waits for a human.

    This test pins the CURRENT behaviour so the limitation is a decision on
    record rather than a surprise during a 200-clinic install."""
    from app.services import maintenance

    fleet = await _fleet(db_session, n=3, prefix="auto")
    for practice in fleet:
        practice.pms_credentials = {"bridge": "nexhealth", "product_key": "pk"}
    await db_session.commit()

    class FakeClient:
        async def list_locations(self):
            return [{"id": f"90{i}", "name": p.name} for i, p in enumerate(fleet)]

    monkeypatch.setattr(
        "app.adapters.nexhealth.client.NexHealthClient", lambda *a, **k: FakeClient()
    )
    monkeypatch.setattr(maintenance.get_settings(), "nexhealth_api_key", "test-key")

    linked = await maintenance.link_synced_locations()
    assert linked == 0, "ambiguity must never be resolved by guessing"
    for practice in fleet:
        await db_session.refresh(practice)
        assert "location_id" not in (practice.pms_credentials or {})


# ── The bridge a 200-clinic Open Dental group needs ────────────────────────


async def test_open_dental_can_be_configured_per_practice():
    """A group running Open Dental cannot be onboarded at all today.

    bridge.REQUIRED_FIELDS knows "nexhealth" and "kolla". There is no way to
    store a per-practice Open Dental customer key, so `practice_credentials`
    returns None for every such clinic and the agent falls back to our own book —
    silently, with the clinic's real calendar sitting right there.

    The per-practice Open Dental client already refuses to start without an
    explicit customer key (so one clinic can never reach another's office); what
    is missing is the row it would read that key from."""
    from app.adapters.bridge import REQUIRED_FIELDS

    assert "open_dental" in REQUIRED_FIELDS, (
        "no per-practice Open Dental bridge — a group on Open Dental has nowhere "
        "to put its customer key"
    )
    assert "customer_key" in REQUIRED_FIELDS["open_dental"]


async def test_an_open_dental_practice_resolves_to_its_own_bridge():
    """And the resolver has to return it, or the credentials are stored and
    ignored — which looks identical to working until a patient is booked into
    nothing."""
    from types import SimpleNamespace

    from app.adapters.bridge import bridge_name

    practice = SimpleNamespace(
        pms_system="open_dental",
        pms_credentials={"bridge": "open_dental", "customer_key": "ck_live_abc"},
    )
    assert bridge_name(practice) == "open_dental"
