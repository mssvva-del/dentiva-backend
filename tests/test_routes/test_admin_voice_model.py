"""Voice-model switch: allowlist, RBAC, Retell wiring (mocked), audit path."""

from __future__ import annotations

import uuid

from sqlalchemy import select

import app.routes.admin as admin_mod
from app.models.dentiva_staff import DentivaStaff
from app.models.practice import Practice
from app.models.user import User
from tests.conftest import seed_practice


async def _internal(db_session, *, clerk_id, role):
    u = User(id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
             email=f"{clerk_id}@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": "org_internal"}


async def _bind_agent(db_session, practice_id, agent_id="agent_vm_1"):
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one()
    p.retell_agent_id = agent_id
    await db_session.commit()


def _fake_retell(monkeypatch, *, model="gpt-4.1"):
    calls = {"set": [], "publish": []}

    async def _agent(agent_id, **_kw):
        return {"agent_id": agent_id, "response_engine": {"llm_id": "llm_vm"}}

    async def _llm(llm_id, **_kw):
        return {"llm_id": llm_id, "model": model}

    async def _set(llm_id, m, **_kw):
        calls["set"].append((llm_id, m))
        return {"model": m}

    async def _pub(agent_id, **_kw):
        calls["publish"].append(agent_id)
        return {}

    monkeypatch.setattr(admin_mod, "get_agent", _agent)
    monkeypatch.setattr(admin_mod, "get_llm", _llm)
    monkeypatch.setattr(admin_mod, "set_llm_model", _set)
    monkeypatch.setattr(admin_mod, "publish_agent", _pub)
    return calls


async def test_get_voice_model(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="eng_vm1", role="engineer")
    practice, _ = await seed_practice(db_session, name="VMCo",
                                      clerk_org_id="o_vm1", clerk_user_id="u_vm1")
    await _bind_agent(db_session, practice.id)
    _fake_retell(monkeypatch, model="gpt-5.1")

    r = await client.get("/api/admin/voice/model", headers=_h("eng_vm1"))
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "gpt-5.1"
    assert "claude-4.5-haiku" in body["allowed"]
    assert "gpt-5.5" not in body["allowed"]  # premium tier stays out (budget rule)


async def test_set_voice_model_switch_and_publish(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="sa_vm2", role="super_admin")
    practice, _ = await seed_practice(db_session, name="VMCo2",
                                      clerk_org_id="o_vm2", clerk_user_id="u_vm2")
    await _bind_agent(db_session, practice.id, "agent_vm_2")
    calls = _fake_retell(monkeypatch)

    r = await client.put("/api/admin/voice/model", headers=_h("sa_vm2"),
                         json={"model": "claude-4.5-haiku"})
    assert r.status_code == 200 and r.json()["model"] == "claude-4.5-haiku"
    assert calls["set"] == [("llm_vm", "claude-4.5-haiku")]
    assert calls["publish"] == ["agent_vm_2"]  # change goes live immediately


async def test_set_voice_model_validates_allowlist(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="sa_vm3", role="super_admin")
    practice, _ = await seed_practice(db_session, name="VMCo3",
                                      clerk_org_id="o_vm3", clerk_user_id="u_vm3")
    await _bind_agent(db_session, practice.id)
    calls = _fake_retell(monkeypatch)

    r = await client.put("/api/admin/voice/model", headers=_h("sa_vm3"),
                         json={"model": "skynet-9000"})
    assert r.status_code == 422
    # premium tiers are valid Retell models but OUT of our budget allowlist —
    # they must be rejected the same way (budget rule, Sergio 2026-07-07)
    prem = await client.put("/api/admin/voice/model", headers=_h("sa_vm3"),
                            json={"model": "gpt-5.5"})
    assert prem.status_code == 422
    son = await client.put("/api/admin/voice/model", headers=_h("sa_vm3"),
                           json={"model": "claude-5-sonnet"})
    assert son.status_code == 422
    assert calls["set"] == []  # nothing ever reached Retell


async def test_voice_model_rbac(client, db_session, monkeypatch):
    # sales/finance lack MANAGE_FEATURE_FLAGS.
    await _internal(db_session, clerk_id="fin_vm", role="finance")
    _fake_retell(monkeypatch)
    for who in ("fin_vm", "random_clinic"):
        g = await client.get("/api/admin/voice/model", headers=_h(who))
        assert g.status_code in (401, 403), who
        p = await client.put("/api/admin/voice/model", headers=_h(who),
                             json={"model": "gpt-5.1"})
        assert p.status_code in (401, 403), who


async def test_voice_model_404_without_agent(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="sa_vm4", role="super_admin")
    _fake_retell(monkeypatch)
    # no practice has retell_agent_id and env is empty in tests
    r = await client.get("/api/admin/voice/model", headers=_h("sa_vm4"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Live-config drift check.
#
# Twice in one day the repo and the live agent disagreed: a prompt fix went to one
# agent while production answered on another, and the voice tuning in the repo was
# never the tuning callers heard. This endpoint reads the real agent and names the
# differences that change what a caller hears.
# ---------------------------------------------------------------------------

_GOOD_PROMPT = (
    "EMERGENCY: tell them to call 911 or go to the nearest emergency room. "
    "Otherwise the team calls back {{callback_eta}} while the office is "
    "{{office_status}}."
)
_BAD_PROMPT = "On request: 'let me connect you with a team member, one moment.'"


def _fake_live(monkeypatch, *, prompt, tools, sensitivity):
    async def _agent(agent_id, **_kw):
        return {"agent_id": agent_id, "response_engine": {"llm_id": "llm_vm"},
                "voice_id": "11labs-Marissa", "voice_model": "eleven_flash_v2_5",
                "interruption_sensitivity": sensitivity,
                "end_call_after_silence_ms": 45000}

    async def _llm(llm_id, **_kw):
        return {"llm_id": llm_id, "model": "gpt-4.1", "general_prompt": prompt,
                "general_tools": tools}

    monkeypatch.setattr(admin_mod, "get_agent", _agent)
    monkeypatch.setattr(admin_mod, "get_llm", _llm)


async def test_live_config_reports_a_clean_agent(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="eng_lc1", role="engineer")
    practice, _ = await seed_practice(db_session, name="LCCo",
                                      clerk_org_id="o_lc1", clerk_user_id="u_lc1")
    await _bind_agent(db_session, practice.id, agent_id="agent_lc_1")
    _fake_live(monkeypatch, prompt=_GOOD_PROMPT, sensitivity=0.65, tools=[
        {"name": "book_appointment", "type": "custom"},
        {"name": "transfer_to_team", "type": "transfer_call"},
    ])

    r = await client.get("/api/admin/voice/live-config", headers=_h("eng_lc1"))
    assert r.status_code == 200
    body = r.json()
    assert body["drift"] == []
    assert body["native_transfer_tools"] == ["transfer_to_team"]
    assert body["has_emergency_room_branch"] is True
    assert body["promises_a_bridge_it_cannot_make"] is False


async def test_live_config_names_every_drift(client, db_session, monkeypatch):
    """An agent stuck on the pre-fix config must be impossible to miss."""
    await _internal(db_session, clerk_id="eng_lc2", role="engineer")
    practice, _ = await seed_practice(db_session, name="LCCo2",
                                      clerk_org_id="o_lc2", clerk_user_id="u_lc2")
    await _bind_agent(db_session, practice.id, agent_id="agent_lc_2")
    _fake_live(monkeypatch, prompt=_BAD_PROMPT, sensitivity=0.8, tools=[
        {"name": "book_appointment", "type": "custom"},
    ])

    r = await client.get("/api/admin/voice/live-config", headers=_h("eng_lc2"))
    assert r.status_code == 200
    body = r.json()
    assert body["promises_a_bridge_it_cannot_make"] is True
    assert body["has_emergency_room_branch"] is False
    assert body["native_transfer_tools"] == []
    joined = " | ".join(body["drift"])
    for expected in ("promises to connect", "emergency-room", "native transfer_call",
                     "interruption_sensitivity", "hours-aware"):
        assert expected in joined, f"drift must name {expected}: {joined}"


async def test_bridge_wording_is_fine_once_a_native_transfer_exists(
    client, db_session, monkeypatch
):
    """"Let me connect you" is only a lie when nothing can bridge the call."""
    await _internal(db_session, clerk_id="eng_lc3", role="engineer")
    practice, _ = await seed_practice(db_session, name="LCCo3",
                                      clerk_org_id="o_lc3", clerk_user_id="u_lc3")
    await _bind_agent(db_session, practice.id, agent_id="agent_lc_3")
    _fake_live(monkeypatch, prompt=_GOOD_PROMPT + " " + _BAD_PROMPT, sensitivity=0.65,
               tools=[{"name": "transfer_to_team", "type": "transfer_call"}])

    r = await client.get("/api/admin/voice/live-config", headers=_h("eng_lc3"))
    assert r.json()["promises_a_bridge_it_cannot_make"] is False
    assert r.json()["drift"] == []


async def test_admin_reads_go_through_the_platform_connection():
    """Cross-clinic admin screens must use the one connection allowed to read
    across tenants. If an admin route quietly goes back to the clinic-facing
    session factory, it returns empty the moment production stops running as
    superuser — and the fix would look like "RLS is broken" instead of a
    mis-wired session."""
    import re
    from pathlib import Path

    src = Path("app/routes/admin.py").read_text()
    stray = re.findall(r"_app_db\.async_session_factory\(\)", src)
    assert not stray, (
        f"{len(stray)} admin queries use the tenant-scoped session factory; "
        "cross-clinic reads belong on platform_session_factory"
    )
    assert "_app_db.platform_session_factory()" in src


async def test_system_health_preflights_the_rls_switch(client, db_session):
    """Before repointing DATABASE_URL at the RLS-enforced role, we need to know
    whether that role can still read every table — otherwise the first symptom is
    a live call dying on "permission denied for table …"."""
    await _internal(db_session, clerk_id="eng_rls1", role="engineer")
    r = await client.get("/api/admin/system-health", headers=_h("eng_rls1"))
    assert r.status_code == 200
    body = r.json()
    assert "rls_enforced" in body and "rls_switch_blockers" in body
    # The test DB connects as the owner (superuser), which is the same shape as
    # production today: not enforced, and the pre-flight has an opinion about it.
    if body["rls_enforced"] is False:
        assert isinstance(body["rls_switch_blockers"], list)
    else:
        # Already enforced → nothing left to pre-flight.
        assert body["rls_switch_blockers"] == []


async def test_the_preflight_cannot_report_the_database_as_down(client, db_session):
    """The first version of this pre-flight threw on a Postgres name-resolution
    quirk, the outer handler turned that into db_ok=False, and the admin page
    announced "Database Unreachable" while the database was serving live calls.
    A diagnostic that can take down the thing it diagnoses is worse than no
    diagnostic."""
    await _internal(db_session, clerk_id="eng_pf1", role="engineer")
    r = await client.get("/api/admin/system-health", headers=_h("eng_pf1"))
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True, "the pre-flight must not be able to flip this"
    # And it must have actually run rather than silently reporting nothing.
    assert body["rls_enforced"] is not None
