from tests.conftest import seed_practice


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
