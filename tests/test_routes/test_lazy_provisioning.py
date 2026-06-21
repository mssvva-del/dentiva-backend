"""Lazy provisioning (staging-enablement, in-spec first-login onboarding).

When LAZY_PROVISIONING is on, a Clerk-authenticated user with no local record is
provisioned from their OWN verified org claim: practice created in 'onboarding'
if missing, user linked with a sensible role. Verified multi-tenant (never the
demo practice). Uses the dev-bypass auth headers to simulate verified claims.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.audit_log import AuditLog
from app.models.practice import Practice
from app.models.user import User
from tests.conftest import seed_practice


@pytest.fixture
def lazy_on(monkeypatch):
    monkeypatch.setenv("LAZY_PROVISIONING", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("LAZY_PROVISIONING", raising=False)
    get_settings.cache_clear()


def _auth(org, user, *, role=None, email=None):
    h = {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}
    if role:
        h["X-Dev-Clerk-Org-Role"] = role
    if email:
        h["X-Dev-Clerk-Email"] = email
    return h


async def test_new_org_creates_practice_in_onboarding_as_owner(client, db_session, lazy_on):
    # Brand-new user with a brand-new org → practice created in onboarding, owner.
    r = await client.get("/api/me", headers=_auth("org_new1", "user_new1",
                                                  email="founder@clinic.com"))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "owner"

    practice = (
        await db_session.execute(
            select(Practice).where(Practice.clerk_org_id == "org_new1")
        )
    ).scalar_one()
    assert practice.status == "onboarding"
    assert practice.onboarding_step == 1

    user = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_new1"))
    ).scalar_one()
    assert user.practice_id == practice.id
    assert user.email == "founder@clinic.com"


async def test_lazy_provisioning_writes_audit_log(client, db_session, lazy_on):
    # S6: the lazy-provisioning path must leave a searchable audit trail.
    r = await client.get("/api/me", headers=_auth("org_audit1", "user_audit1",
                                                  email="founder@audit.com"))
    assert r.status_code == 200

    practice = (
        await db_session.execute(
            select(Practice).where(Practice.clerk_org_id == "org_audit1")
        )
    ).scalar_one()
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.practice_id == practice.id,
                AuditLog.action == "lazy_provisioning",
            )
        )
    ).scalar_one()
    assert audit.resource_type == "user"
    assert audit.audit_metadata["practice_created"] is True
    assert audit.audit_metadata["role"] == "owner"
    assert audit.audit_metadata["clerk_org_id"] == "org_audit1"


async def test_join_existing_practice_as_staff(client, db_session, lazy_on):
    # Practice already has an owner → a second user (org member) becomes staff.
    await seed_practice(db_session, name="Existing", clerk_org_id="org_exist",
                        clerk_user_id="user_owner_x")
    r = await client.get("/api/me", headers=_auth("org_exist", "user_second",
                                                  role="org:member"))
    assert r.status_code == 200
    assert r.json()["role"] == "staff"


async def test_join_existing_as_admin_role_becomes_owner(client, db_session, lazy_on):
    await seed_practice(db_session, name="Existing2", clerk_org_id="org_exist2",
                        clerk_user_id="user_owner_y")
    # Wipe the seeded owner so there's no active owner → first joiner becomes owner.
    u = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_owner_y"))
    ).scalar_one()
    u.status = "disabled"
    await db_session.commit()
    r = await client.get("/api/me", headers=_auth("org_exist2", "user_admin_z",
                                                  role="org:admin"))
    assert r.json()["role"] == "owner"


async def test_no_org_claim_is_unauthorized(client, db_session, lazy_on):
    # No org → can't place the user in a clinic → 401 (frontend creates org first).
    r = await client.get("/api/me", headers={"X-Dev-Clerk-User-Id": "user_no_org"})
    assert r.status_code == 401


async def test_lazy_off_by_default_still_401(client, db_session):
    # Without the flag, an unknown user is NOT provisioned (prod-safe default).
    r = await client.get("/api/me", headers=_auth("org_off", "user_off"))
    assert r.status_code == 401


async def test_multitenant_not_attached_to_demo(client, db_session, lazy_on):
    # A pre-existing "demo" practice must NOT capture a new org's user.
    await seed_practice(db_session, name="Demo", clerk_org_id="org_demo",
                        clerk_user_id="user_demo_owner")
    r = await client.get("/api/me", headers=_auth("org_brand_new", "user_brand_new"))
    assert r.status_code == 200
    user = (
        await db_session.execute(
            select(User).where(User.clerk_user_id == "user_brand_new")
        )
    ).scalar_one()
    new_practice = (
        await db_session.execute(
            select(Practice).where(Practice.clerk_org_id == "org_brand_new")
        )
    ).scalar_one()
    # Linked to its OWN new practice, not the demo one.
    assert user.practice_id == new_practice.id
