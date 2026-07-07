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
