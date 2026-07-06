"""Ring-count / AI answer config — call_routing logic + practice settings API."""

from __future__ import annotations

import uuid

from app.services.call_routing import (
    AFTER_HOURS,
    FULL_TIME,
    OVERFLOW,
    forwarding_instruction,
    rings_to_seconds,
)
from tests.conftest import seed_practice


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:8]}"


# ── pure logic ──────────────────────────────────────────────────────────────
def test_rings_to_seconds_applies_carrier_minimum():
    assert rings_to_seconds(1) == 14   # 6s < 14s minimum → 14
    assert rings_to_seconds(3) == 18
    assert rings_to_seconds(5) == 30


def test_forwarding_instruction_per_mode():
    num = "+15550001111"
    full = forwarding_instruction(answer_mode=FULL_TIME, rings_before_ai=3, ai_number=num)
    assert "main line" in full and "immediately" in full and num in full

    over = forwarding_instruction(answer_mode=OVERFLOW, rings_before_ai=3, ai_number=num)
    assert "Conditional Call Forwarding" in over and "3 rings" in over

    ah = forwarding_instruction(answer_mode=AFTER_HOURS, rings_before_ai=2, ai_number=None)
    assert "outside business hours" in ah and "Dentovox number" in ah  # placeholder


# ── settings API ──────────────────────────────────────────────────────────
def _hdr(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def test_practice_me_exposes_ring_config(client, db_session):
    org, user = _uid("org_cr"), _uid("user_cr")
    await seed_practice(db_session, name="Ring Co", clerk_org_id=org, clerk_user_id=user)
    resp = await client.get("/api/practice/me", headers=_hdr(org, user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer_mode"] == "overflow"
    assert body["rings_before_ai"] == 3
    assert "Conditional Call Forwarding" in body["forwarding_instruction"]


async def test_patch_ring_config(client, db_session):
    org, user = _uid("org_cr2"), _uid("user_cr2")
    await seed_practice(db_session, name="Ring Co 2", clerk_org_id=org, clerk_user_id=user)
    resp = await client.patch(
        "/api/practice/me",
        headers=_hdr(org, user),
        json={"answer_mode": "full_time", "rings_before_ai": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer_mode"] == "full_time" and body["rings_before_ai"] == 5
    # full_time instruction changes to "answers immediately"
    assert "immediately" in body["forwarding_instruction"]


async def test_patch_rejects_bad_mode_and_rings(client, db_session):
    org, user = _uid("org_cr3"), _uid("user_cr3")
    await seed_practice(db_session, name="Ring Co 3", clerk_org_id=org, clerk_user_id=user)
    bad_mode = await client.patch(
        "/api/practice/me", headers=_hdr(org, user), json={"answer_mode": "bogus"}
    )
    assert bad_mode.status_code == 400
    bad_rings = await client.patch(
        "/api/practice/me", headers=_hdr(org, user), json={"rings_before_ai": 99}
    )
    assert bad_rings.status_code == 400
