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


async def test_practice_me_unknown_user(client):
    resp = await client.get(
        "/api/practice/me",
        headers={"X-Dev-Clerk-User-Id": "ghost", "X-Dev-Clerk-Org-Id": "org_x"},
    )
    assert resp.status_code == 401


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
