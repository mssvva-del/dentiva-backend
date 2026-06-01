"""DEMO_OPEN_ACCESS: an unknown Clerk user auto-joins the demo practice."""

from __future__ import annotations

from app.config import get_settings
from tests.conftest import seed_practice


def _auth(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def test_unknown_user_401_when_demo_off(client, db_session, monkeypatch):
    await seed_practice(
        db_session, name="Demo Off", clerk_org_id="org_do1", clerk_user_id="user_do1"
    )
    monkeypatch.setenv("DEMO_OPEN_ACCESS", "false")
    get_settings.cache_clear()
    try:
        # A brand-new clerk user id that was never provisioned.
        resp = await client.get("/api/practice/me", headers=_auth("org_x", "stranger_1"))
        assert resp.status_code == 401
    finally:
        get_settings.cache_clear()


async def test_unknown_user_auto_joins_demo_when_on(client, db_session, monkeypatch):
    practice, _ = await seed_practice(
        db_session, name="Demo On", clerk_org_id="org_do2", clerk_user_id="user_do2"
    )
    monkeypatch.setenv("DEMO_OPEN_ACCESS", "true")
    get_settings.cache_clear()
    try:
        # Brand-new user, never provisioned → should be auto-attached to the
        # demo (first) practice and get a working response.
        resp = await client.get(
            "/api/practice/me", headers=_auth("org_x", "stranger_demo_2")
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Demo On"
    finally:
        get_settings.cache_clear()
