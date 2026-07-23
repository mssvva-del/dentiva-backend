import uuid

from tests.conftest import seed_practice


def _uid(prefix: str) -> str:
    """Return a prefix + short UUID suffix to avoid cross-test unique key collisions."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def test_practice_me_requires_auth(client):
    # Dev bypass on, but no X-Dev header -> 401.
    resp = await client.get("/api/practice/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_practice_me_happy_path(client, db_session):
    practice, user = await seed_practice(
        db_session, name="Smile Dental NJ", clerk_org_id="org_A", clerk_user_id="user_A"
    )
    resp = await client.get(
        "/api/practice/me",
        headers={"X-Dev-Clerk-User-Id": "user_A", "X-Dev-Clerk-Org-Id": "org_A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Smile Dental NJ"
    assert body["languages_enabled"] == ["en"]
    assert body["pms_connected"] is False
    assert "business_hours" in body


async def test_practice_me_provisions_new_user(client):
    # A brand-new signed-in user with no practice is auto-provisioned into an
    # onboarding practice (the signup flow), not rejected.
    resp = await client.get(
        "/api/practice/me",
        headers={"X-Dev-Clerk-User-Id": "ghost", "X-Dev-Clerk-Org-Id": "org_ghost"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"]  # a fresh practice now exists for them


# ---------------------------------------------------------------------------
# PATCH /api/practice/me tests
# ---------------------------------------------------------------------------


async def test_patch_practice_me_requires_auth(client):
    resp = await client.patch("/api/practice/me", json={"name": "New Name"})
    assert resp.status_code == 401


async def test_patch_practice_me_update_name_only(client, db_session):
    org_id = _uid("org_patch1")
    user_id = _uid("user_patch1")
    practice, user = await seed_practice(
        db_session,
        name="Original Name",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )
    resp = await client.patch(
        "/api/practice/me",
        json={"name": "Updated Name"},
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Updated Name"
    # Other fields should remain unchanged.
    assert body["timezone"] == "America/New_York"
    assert body["languages_enabled"] == ["en"]


async def test_patch_practice_me_update_multiple_fields(client, db_session):
    org_id = _uid("org_patch2")
    user_id = _uid("user_patch2")
    practice, user = await seed_practice(
        db_session,
        name="Multi Field Practice",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )
    resp = await client.patch(
        "/api/practice/me",
        json={
            "name": "New Multi Name",
            "timezone": "America/Chicago",
            "languages_enabled": ["en", "es"],
        },
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New Multi Name"
    assert body["timezone"] == "America/Chicago"
    assert body["languages_enabled"] == ["en", "es"]


async def test_patch_practice_me_update_address(client, db_session):
    # Doctor edits the clinic address post-onboarding (Settings → Practice Identity).
    org_id = _uid("org_addr1")
    user_id = _uid("user_addr1")
    await seed_practice(
        db_session, name="Addr Practice", clerk_org_id=org_id, clerk_user_id=user_id
    )
    resp = await client.patch(
        "/api/practice/me",
        json={"address": "500 Elm St, Suite 4, Denver, CO 80202"},
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    assert resp.json()["address"] == "500 Elm St, Suite 4, Denver, CO 80202"
    # persisted + surfaced on the next GET
    getr = await client.get(
        "/api/practice/me",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert getr.json()["address"] == "500 Elm St, Suite 4, Denver, CO 80202"


async def test_patch_practice_me_empty_body_returns_200_unchanged(client, db_session):
    org_id = _uid("org_patch3")
    user_id = _uid("user_patch3")
    practice, user = await seed_practice(
        db_session,
        name="No Change Practice",
        clerk_org_id=org_id,
        clerk_user_id=user_id,
    )
    resp = await client.patch(
        "/api/practice/me",
        json={},
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "No Change Practice"
    assert body["timezone"] == "America/New_York"


async def test_practice_me_exposes_reminders_enabled_default_true(client, db_session):
    org_id = _uid("org_rem_def")
    user_id = _uid("user_rem_def")
    await seed_practice(
        db_session, name="Rem Default", clerk_org_id=org_id, clerk_user_id=user_id
    )
    resp = await client.get(
        "/api/practice/me",
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    assert resp.json()["reminders_enabled"] is True


async def test_patch_practice_me_toggle_reminders(client, db_session):
    org_id = _uid("org_rem_tog")
    user_id = _uid("user_rem_tog")
    await seed_practice(
        db_session, name="Rem Toggle", clerk_org_id=org_id, clerk_user_id=user_id
    )
    resp = await client.patch(
        "/api/practice/me",
        json={"reminders_enabled": False},
        headers={"X-Dev-Clerk-User-Id": user_id, "X-Dev-Clerk-Org-Id": org_id},
    )
    assert resp.status_code == 200
    assert resp.json()["reminders_enabled"] is False


async def test_practice_me_exposes_agent_persona_and_forwarding(client, db_session):
    """TRUST-FIX: the Settings card must show the REAL persona the live agent
    uses (same defaults as build_dynamic_variables) plus the forward-to number —
    never hardcoded UI values."""
    from sqlalchemy import select

    from app.models.practice import Practice as P

    practice, _ = await seed_practice(
        db_session, name="Persona Me Co", clerk_org_id="org_pm", clerk_user_id="user_pm"
    )
    h = {"X-Dev-Clerk-User-Id": "user_pm", "X-Dev-Clerk-Org-Id": "org_pm"}

    # Defaults (no agent_settings yet) → "Alex", no greeting.
    body = (await client.get("/api/practice/me", headers=h)).json()
    assert body["agent_name"] == "Alex"
    assert body["agent_greeting"] is None
    assert "forwarding_instruction" in body and "ai_phone_number" in body

    # Onboarding step-5 shape flows through.
    row = (await db_session.execute(select(P).where(P.id == practice.id))).scalar_one()
    row.agent_settings = {"agent_name": "Sofia", "voice": "x", "greeting": "Welcome!"}
    await db_session.commit()
    body2 = (await client.get("/api/practice/me", headers=h)).json()
    assert body2["agent_name"] == "Sofia"
    assert body2["agent_greeting"] == "Welcome!"


async def test_patch_practice_me_updates_agent_persona(client, db_session):
    """PATCH agent_name/agent_greeting persists into agent_settings (the same
    JSONB the live-call dynamic variables read) and is audited."""
    practice, user = await seed_practice(
        db_session, name="Persona Patch Co", clerk_org_id="org_pp", clerk_user_id="user_pp"
    )
    h = {"X-Dev-Clerk-User-Id": "user_pp", "X-Dev-Clerk-Org-Id": "org_pp"}

    resp = await client.patch(
        "/api/practice/me", headers=h,
        json={"agent_name": "Maya", "agent_greeting": "We can't wait to see you!"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_name"] == "Maya"
    assert body["agent_greeting"] == "We can't wait to see you!"

    # Clearing the greeting with an empty string works (optional line removed).
    resp2 = await client.patch("/api/practice/me", headers=h, json={"agent_greeting": ""})
    assert resp2.status_code == 200
    assert resp2.json()["agent_greeting"] is None
    assert resp2.json()["agent_name"] == "Maya"  # untouched

    # And the live-call variables see the same values (single source of truth).
    from sqlalchemy import select

    from app.models.practice import Practice as P
    from app.services.llm.dynamic_vars import build_dynamic_variables
    row = (await db_session.execute(select(P).where(P.id == practice.id))
           ).scalar_one()
    await db_session.refresh(row)
    v = build_dynamic_variables(row)
    assert v["agent_name"] == "Maya"
    assert v["custom_greeting"] == ""
