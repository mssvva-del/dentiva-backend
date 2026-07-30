"""V1 — inbound-call webhook: KB → dynamic variables for the live agent."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from sqlalchemy import select

from app.models.practice import Practice
from app.services.llm.dynamic_vars import build_dynamic_variables
from tests.conftest import seed_practice


def _sign(secret: str, body: bytes, ts_ms: int) -> str:
    digest = hmac.new(secret.encode(), body + str(ts_ms).encode(), hashlib.sha256).hexdigest()
    return f"v={ts_ms},d={digest}"


# ── variable builder ─────────────────────────────────────────────────────────
async def test_build_dynamic_variables_shape(db_session):
    practice, _ = await seed_practice(db_session, name="Bright Smiles NJ",
                                      clerk_org_id="o_dv1", clerk_user_id="u_dv1")
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    p.timezone = "America/New_York"
    p.knowledge_base = {
        "providers": [{"name": "Dr. Chen", "type": "dentist", "accepts_new": True}],
        "insurances": ["Delta Dental", "Cigna"],
        "self_pay": True,
        "policies": {"parking": "Free lot behind the building."},
    }
    await db_session.commit()

    v = build_dynamic_variables(p)
    # Retell requires string values only.
    assert all(isinstance(x, str) for x in v.values())
    assert v["practice_name"] == "Bright Smiles NJ"
    assert "Dr. Chen" in v["kb_context"]
    assert "Delta Dental" in v["kb_context"]
    assert v["timezone"] == "America/New_York"
    assert "," in v["today"]          # "Monday, July 6, 2026"
    assert ("AM" in v["current_time"]) or ("PM" in v["current_time"])


async def test_build_dynamic_variables_agent_persona(db_session):
    """D1: onboarding step-5 persona reaches the live call. Custom name/greeting
    flow through; unset falls back to 'Alex' + EMPTY greeting (keys must always
    exist — a missing key would leave literal '{{agent_name}}' spoken aloud)."""
    practice, _ = await seed_practice(db_session, name="Persona Co",
                                      clerk_org_id="o_dv6", clerk_user_id="u_dv6")
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()

    # Unset agent_settings → defaults.
    v = build_dynamic_variables(p)
    assert v["agent_name"] == "Alex"
    assert v["custom_greeting"] == ""

    # Clinic customized name + greeting (step 5 shape).
    p.agent_settings = {"agent_name": "Sofia", "voice": "cartesia-Hailey",
                        "greeting": "We're excited to see your smile!"}
    await db_session.commit()
    v2 = build_dynamic_variables(p)
    assert v2["agent_name"] == "Sofia"
    assert v2["custom_greeting"] == "We're excited to see your smile!"
    # Bounded (Retell prompt hygiene).
    p.agent_settings = {"agent_name": "X" * 200, "greeting": "Y" * 900}
    v3 = build_dynamic_variables(p)
    assert len(v3["agent_name"]) <= 60 and len(v3["custom_greeting"]) <= 300


async def test_build_dynamic_variables_empty_kb(db_session):
    practice, _ = await seed_practice(db_session, name="Empty KB Co",
                                      clerk_org_id="o_dv2", clerk_user_id="u_dv2")
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    v = build_dynamic_variables(p)
    assert v["kb_context"] == "No additional clinic details on file."


# ── endpoint ─────────────────────────────────────────────────────────────────
async def test_inbound_webhook_returns_variables(client, db_session, monkeypatch):
    import app.webhooks.retell as retell_mod

    class _Cfg:
        retell_webhook_secret = "key_test_inbound"
        environment = "production"
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Cfg())

    practice, _ = await seed_practice(db_session, name="Inbound Co",
                                      clerk_org_id="o_dv3", clerk_user_id="u_dv3")
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    p.retell_agent_id = "agent_inbound_1"
    p.knowledge_base = {"insurances": ["Aetna"]}
    await db_session.commit()

    body = json.dumps({
        "event": "call_inbound",
        "call_inbound": {"agent_id": "agent_inbound_1",
                         "from_number": "+17325550000",
                         "to_number": "+16204559562"},
    }).encode()
    ts = int(time.time() * 1000)  # fresh timestamp — inside the 5-min window
    r = await client.post(
        "/webhooks/retell/inbound", content=body,
        headers={"x-retell-signature": _sign("key_test_inbound", body, ts),
                 "content-type": "application/json"},
    )
    assert r.status_code == 200
    vars_ = r.json()["call_inbound"]["dynamic_variables"]
    assert vars_["practice_name"] == "Inbound Co"
    assert "Aetna" in vars_["kb_context"]

    # tampered signature → 401
    bad = await client.post("/webhooks/retell/inbound", content=body,
                            headers={"x-retell-signature": "v=1,d=bad",
                                     "content-type": "application/json"})
    assert bad.status_code == 401


async def test_inbound_webhook_dev_mode_no_secret(client, db_session, monkeypatch):
    """Dev (no secret): signature skipped; variables resolve via agent binding."""
    import app.webhooks.retell as retell_mod

    class _Dev:
        retell_webhook_secret = ""
        environment = "development"
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Dev())

    practice, _ = await seed_practice(db_session, name="DevMode Clinic",
                                      clerk_org_id="o_dv4", clerk_user_id="u_dv4")
    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    p.retell_agent_id = "agent_dev_9"
    await db_session.commit()

    body = json.dumps({"event": "call_inbound",
                       "call_inbound": {"agent_id": "agent_dev_9"}}).encode()
    r = await client.post("/webhooks/retell/inbound", content=body,
                          headers={"content-type": "application/json"})
    assert r.status_code == 200
    vars_ = r.json()["call_inbound"]["dynamic_variables"]
    assert vars_["practice_name"] == "DevMode Clinic"


async def test_inbound_webhook_unknown_agent_returns_empty_when_multi(client,
                                                                      db_session,
                                                                      monkeypatch):
    """2+ practices and no binding → ambiguous → EMPTY vars (never wrong clinic),
    and the call still gets answered (200)."""
    import app.webhooks.retell as retell_mod

    class _Dev:
        retell_webhook_secret = ""
        environment = "development"
    monkeypatch.setattr(retell_mod, "get_settings", lambda: _Dev())

    await seed_practice(db_session, name="Multi A", clerk_org_id="o_dv5a",
                        clerk_user_id="u_dv5a")
    await seed_practice(db_session, name="Multi B", clerk_org_id="o_dv5b",
                        clerk_user_id="u_dv5b")

    body = json.dumps({"event": "call_inbound",
                       "call_inbound": {"agent_id": "agent_nobody"}}).encode()
    r = await client.post("/webhooks/retell/inbound", content=body,
                          headers={"content-type": "application/json"})
    assert r.status_code == 200
    variables = r.json()["call_inbound"]["dynamic_variables"]
    # No clinic identity may leak when the clinic is ambiguous.
    assert "practice_name" not in variables
    assert "kb_context" not in variables
    # But the two variables the emergency branch SPEAKS must still be present:
    # Retell substitutes only the keys we send, so a missing one is read aloud as
    # a literal "{{callback_eta}}" to someone describing an emergency.
    assert variables == {
        "office_status": "open",
        "callback_eta": "shortly",
        # Empty: with no clinic resolved there is no line to transfer to, and the
        # prompt keeps the agent on the callback path.
        "clinic_transfer_number": "",
    }
