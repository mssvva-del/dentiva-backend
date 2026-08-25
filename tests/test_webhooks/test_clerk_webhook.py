"""Phase B1 — Clerk webhook: svix verification + provisioning.

Covers:
  * svix_verify unit: valid / tampered / stale / missing / rotated-secret
  * /webhooks/clerk provisioning for each modeled event (dev mode, no secret)
  * signature enforcement when CLERK_WEBHOOK_SECRET is set (401 on bad sig)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.practice import Practice
from app.models.user import User
from app.webhooks.svix_verify import SvixVerificationError, verify

# A throwaway signing secret (base64 of 24 bytes), whsec-prefixed like Clerk's.
_TEST_SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef01234567").decode()


def _sign(secret: str, svix_id: str, ts: str, body: bytes) -> str:
    key = base64.b64decode(secret[len("whsec_") :])
    signed = b".".join([svix_id.encode(), ts.encode(), body])
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{sig}"


def _headers(secret: str, body: bytes, *, svix_id="msg_1", ts="1700000000"):
    return {
        "svix-id": svix_id,
        "svix-timestamp": ts,
        "svix-signature": _sign(secret, svix_id, ts, body),
    }


# ── svix_verify unit ─────────────────────────────────────────────────────────
def test_svix_valid_signature_passes():
    body = b'{"hello":"world"}'
    h = _headers(_TEST_SECRET, body)
    # now pinned near the timestamp so the tolerance check passes
    verify(
        secret=_TEST_SECRET, raw_body=body,
        svix_id=h["svix-id"], svix_timestamp=h["svix-timestamp"],
        svix_signature=h["svix-signature"], now=1700000001,
    )


def test_svix_tampered_body_fails():
    body = b'{"hello":"world"}'
    h = _headers(_TEST_SECRET, body)
    with pytest.raises(SvixVerificationError):
        verify(
            secret=_TEST_SECRET, raw_body=b'{"hello":"evil"}',
            svix_id=h["svix-id"], svix_timestamp=h["svix-timestamp"],
            svix_signature=h["svix-signature"], now=1700000001,
        )


def test_svix_stale_timestamp_fails():
    body = b"{}"
    h = _headers(_TEST_SECRET, body, ts="1700000000")
    with pytest.raises(SvixVerificationError):
        verify(
            secret=_TEST_SECRET, raw_body=body,
            svix_id=h["svix-id"], svix_timestamp=h["svix-timestamp"],
            svix_signature=h["svix-signature"], now=1700000000 + 10_000,
        )


def test_svix_missing_headers_fails():
    with pytest.raises(SvixVerificationError):
        verify(
            secret=_TEST_SECRET, raw_body=b"{}",
            svix_id=None, svix_timestamp=None, svix_signature=None,
        )


def test_svix_multiple_signatures_one_matches():
    body = b"{}"
    good = _headers(_TEST_SECRET, body)["svix-signature"]
    multi = f"v1,deadbeef {good}"  # old (rotated) sig + the valid one
    verify(
        secret=_TEST_SECRET, raw_body=body,
        svix_id="msg_1", svix_timestamp="1700000000",
        svix_signature=multi, now=1700000001,
    )


# ── webhook provisioning (dev mode, no secret → signature skipped) ───────────
async def test_user_created_provisions_user(client, db_session):
    body = json.dumps({
        "type": "user.created",
        "data": {
            "id": "user_wh1",
            "primary_email_address_id": "idem1",
            "email_addresses": [{"id": "idem1", "email_address": "wh1@clinic.com"}],
        },
    }).encode()
    resp = await client.post("/webhooks/clerk", content=body)
    assert resp.status_code == 200
    assert resp.json()["result"] == "created"

    u = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_wh1"))
    ).scalar_one()
    assert u.email == "wh1@clinic.com"
    assert u.practice_id is None  # no org yet
    assert u.role == "viewer"     # least privilege until membership


async def test_user_created_is_idempotent(client, db_session):
    body = json.dumps({
        "type": "user.created",
        "data": {"id": "user_wh_idem", "email_addresses": []},
    }).encode()
    r1 = await client.post("/webhooks/clerk", content=body)
    r2 = await client.post("/webhooks/clerk", content=body)
    assert r1.json()["result"] == "created"
    assert r2.json()["result"] == "noop"
    rows = (
        await db_session.execute(
            select(User).where(User.clerk_user_id == "user_wh_idem")
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_org_then_membership_links_owner(client, db_session):
    # 1) org created → practice in onboarding
    org_body = json.dumps({
        "type": "organization.created",
        "data": {"id": "org_wh2", "name": "Bright Smiles"},
    }).encode()
    r = await client.post("/webhooks/clerk", content=org_body)
    assert r.status_code == 200
    p = (
        await db_session.execute(
            select(Practice).where(Practice.clerk_org_id == "org_wh2")
        )
    ).scalar_one()
    assert p.status == "onboarding"
    assert p.onboarding_step == 1
    assert set(p.languages_enabled) == {"en", "es"}

    # 2) membership created (admin) before any user.created → user is created+linked
    mem_body = json.dumps({
        "type": "organizationMembership.created",
        "data": {
            "organization": {"id": "org_wh2"},
            "public_user_data": {"user_id": "user_wh2", "identifier": "boss@bs.com"},
            "role": "org:admin",
        },
    }).encode()
    r = await client.post("/webhooks/clerk", content=mem_body)
    assert r.status_code == 200
    u = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_wh2"))
    ).scalar_one()
    assert u.practice_id == p.id
    assert u.role == "owner"  # admin → owner


async def test_membership_member_maps_to_staff(client, db_session):
    await client.post("/webhooks/clerk", content=json.dumps({
        "type": "organization.created",
        "data": {"id": "org_wh3", "name": "Care Dental"},
    }).encode())
    await client.post("/webhooks/clerk", content=json.dumps({
        "type": "organizationMembership.created",
        "data": {
            "organization": {"id": "org_wh3"},
            "public_user_data": {"user_id": "user_wh3", "identifier": "tech@cd.com"},
            "role": "org:member",
        },
    }).encode())
    u = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_wh3"))
    ).scalar_one()
    assert u.role == "staff"  # member → staff


async def test_user_deleted_soft_disables(client, db_session):
    await client.post("/webhooks/clerk", content=json.dumps({
        "type": "user.created",
        "data": {"id": "user_wh4", "email_addresses": []},
    }).encode())
    r = await client.post("/webhooks/clerk", content=json.dumps({
        "type": "user.deleted",
        "data": {"id": "user_wh4", "deleted": True},
    }).encode())
    assert r.json()["result"] == "disabled"
    u = (
        await db_session.execute(select(User).where(User.clerk_user_id == "user_wh4"))
    ).scalar_one()
    assert u.status == "disabled"  # soft, not removed


async def test_unknown_event_ignored(client):
    r = await client.post("/webhooks/clerk", content=json.dumps({
        "type": "session.created", "data": {},
    }).encode())
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


# ── signature enforcement when a secret IS configured ────────────────────────
async def test_bad_signature_rejected_when_secret_set(client, monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", _TEST_SECRET)
    get_settings.cache_clear()
    try:
        body = json.dumps({"type": "user.created", "data": {"id": "x"}}).encode()
        # wrong signature
        resp = await client.post(
            "/webhooks/clerk",
            content=body,
            headers={"svix-id": "m", "svix-timestamp": "1700000000",
                     "svix-signature": "v1,not-a-real-sig"},
        )
        assert resp.status_code == 401
    finally:
        monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
        get_settings.cache_clear()


async def test_user_updated_learns_the_real_email(client, db_session):
    """user.created can arrive before Clerk has an address, and we store
    "<clerk_id>@unknown.clerk" to satisfy NOT NULL. Ignoring user.updated made
    that placeholder permanent — the admin screen showed a machine id where the
    practice owner's email belongs, and nobody could contact the clinic from the
    record that exists to describe it."""
    from app.models.user import User
    from app.services.clerk_provisioning import (
        handle_user_created,
        handle_user_updated,
    )

    await handle_user_created(db_session, {"id": "user_late_email"})
    await db_session.commit()
    user = (await db_session.execute(
        select(User).where(User.clerk_user_id == "user_late_email")
    )).scalar_one()
    assert user.email == "user_late_email@unknown.clerk"

    result = await handle_user_updated(db_session, {
        "id": "user_late_email",
        "primary_email_address_id": "idn_1",
        "email_addresses": [{"id": "idn_1", "email_address": "dr@harborside.example"}],
    })
    await db_session.commit()
    await db_session.refresh(user)
    assert result == "email_updated"
    assert user.email == "dr@harborside.example"


async def test_user_updated_never_moves_a_user_between_clinics(client, db_session):
    """Deliberately narrow. A profile edit must not be able to change who a user
    works for, or what they are allowed to do."""
    from app.services.clerk_provisioning import handle_user_updated
    from tests.conftest import seed_practice

    practice, owner = await seed_practice(
        db_session, name="Narrow Co", clerk_org_id="org_nar", clerk_user_id="u_nar"
    )
    before_role, before_practice = owner.role, owner.practice_id

    await handle_user_updated(db_session, {
        "id": "u_nar",
        "primary_email_address_id": "idn_2",
        "email_addresses": [{"id": "idn_2", "email_address": "new@example.com"}],
        # Everything below must be ignored.
        "public_metadata": {"dentiva_role": "support"},
        "organization_memberships": [{"organization": {"id": "org_somewhere_else"}}],
    })
    await db_session.commit()
    await db_session.refresh(owner)
    assert owner.email == "new@example.com"
    assert owner.role == before_role
    assert owner.practice_id == before_practice
    assert owner.is_internal is False


def test_both_placeholder_forms_are_recognised():
    """Two paths invent an address: the webhook writes "@unknown.clerk", and a
    user auto-created on their first authenticated request (a JWT carrying no
    email claim) gets "@clerk.local". Healing only one is how the first real
    clinic still showed a machine id after the fix shipped."""
    from app.services.clerk_provisioning import is_placeholder_email

    assert is_placeholder_email("user_abc@unknown.clerk")
    assert is_placeholder_email("User_3lPZuR3GK0pO2W1Zi@clerk.local")
    assert not is_placeholder_email("dr.roe@harborside-dental.com")
    assert not is_placeholder_email(None)
