"""In-app assistant — auth, graceful degradation, and no PHI in the prompt."""

from __future__ import annotations

import httpx

from app.services.assistant.knowledge import SYSTEM_PROMPT
from tests.conftest import seed_practice

_HDR = {"X-Dev-Clerk-User-Id": "u_as", "X-Dev-Clerk-Org-Id": "org_as"}


async def test_requires_auth(client):
    r = await client.post("/api/assistant/ask", json={"message": "how do I forward calls?"})
    assert r.status_code in (401, 403)


async def test_degrades_without_key(client, db_session, monkeypatch):
    await seed_practice(db_session, name="AsstA", clerk_org_id="org_as", clerk_user_id="u_as")
    await db_session.commit()
    import app.routes.assistant as mod
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"anthropic_api_key": ""})())
    r = await client.post("/api/assistant/ask", json={"message": "hi"}, headers=_HDR)
    assert r.status_code == 503  # says so, doesn't crash


async def test_answers_and_sends_no_patient_data(client, db_session, monkeypatch):
    practice, _ = await seed_practice(
        db_session, name="AsstB", clerk_org_id="org_as", clerk_user_id="u_as")
    await db_session.commit()
    import app.routes.assistant as mod
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())

    sent: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"type": "text", "text": "Dial *71 then your Dentovox number."}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    r = await client.post(
        "/api/assistant/ask",
        json={"message": "how do I forward my phone?", "history": []},
        headers=_HDR,
    )
    assert r.status_code == 200
    assert "*71" in r.json()["reply"]
    # The prompt carries the product doc + clinic NAME only — no patient data,
    # no ids, nothing that could turn this endpoint into a PHI leak.
    assert sent["system"].startswith(SYSTEM_PROMPT[:80])
    assert practice.name in sent["system"]
    assert str(practice.id) not in sent["system"]
    assert sent["model"] == "claude-sonnet-5"


async def test_history_is_capped(client, db_session, monkeypatch):
    await seed_practice(db_session, name="AsstC", clerk_org_id="org_as", clerk_user_id="u_as")
    await db_session.commit()
    import app.routes.assistant as mod
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())
    sent: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"type": "text", "text": "ok"}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
               for i in range(40)]
    r = await client.post(
        "/api/assistant/ask", json={"message": "and now?", "history": history}, headers=_HDR)
    assert r.status_code == 200
    assert len(sent["messages"]) <= 13  # 12 kept turns + the new question


def _stub_anthropic(monkeypatch, sent: dict, text: str = "Yes — it books into your calendar."):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"type": "text", "text": text}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())


async def test_public_ask_needs_no_account(client, monkeypatch):
    """The whole point: a dentist evaluating us at 11pm can ask something without
    signing up first."""
    import app.routes.assistant as mod

    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())
    sent: dict = {}
    _stub_anthropic(monkeypatch, sent)

    r = await client.post(
        "/api/assistant/public-ask",
        json={"message": "does it work with Eaglesoft?", "history": []},
    )
    assert r.status_code == 200
    assert r.json()["reply"]


async def test_public_ask_carries_no_clinic_identity(client, db_session, monkeypatch):
    """An open endpoint that accepted a practice name would let a stranger address
    any clinic. There is nobody to name here, so nothing about any practice may
    reach the prompt."""
    import app.routes.assistant as mod
    from app.services.assistant.knowledge import PUBLIC_SYSTEM_PROMPT

    practice, _ = await seed_practice(
        db_session, name="Nameable Dental", clerk_org_id="org_pub", clerk_user_id="u_pub"
    )
    await db_session.commit()

    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())
    sent: dict = {}
    _stub_anthropic(monkeypatch, sent)

    await client.post(
        "/api/assistant/public-ask",
        json={"message": "who am I?", "history": []},
        # Even WITH a clinic's headers, the public endpoint must stay anonymous.
        headers={"X-Dev-Clerk-User-Id": "u_pub", "X-Dev-Clerk-Org-Id": "org_pub"},
    )
    assert sent["system"] == PUBLIC_SYSTEM_PROMPT
    assert "Nameable Dental" not in sent["system"]
    assert str(practice.id) not in sent["system"]


async def test_public_ask_degrades_without_key(client, monkeypatch):
    import app.routes.assistant as mod

    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"anthropic_api_key": ""})())
    r = await client.post("/api/assistant/public-ask", json={"message": "hi"})
    assert r.status_code == 503
