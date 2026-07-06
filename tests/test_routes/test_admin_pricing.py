"""ADM6 — editable pricing/config: public read + admin edit, RBAC, validation."""

from __future__ import annotations

import uuid

from app.models.dentiva_staff import DentivaStaff
from app.models.pricing_plan import PricingPlan
from app.models.user import User


async def _internal(db_session, *, clerk_id, role):
    u = User(id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
             email=f"{clerk_id}@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": "org_internal"}


async def _seed_plan(db_session, *, key="after_hours", monthly=23900, active=True,
                     sort=0, highlight=False):
    p = PricingPlan(
        id=uuid.uuid4(), plan_key=key, name=key.title(), monthly_cents=monthly,
        annual_monthly_cents=round(monthly * 0.84), soft_cap_minutes=1500,
        overage_cents_per_min=18, reactivation_level="basic", per_location=False,
        highlight=highlight, sort_order=sort, is_active=active,
    )
    db_session.add(p)
    await db_session.commit()
    return p


# ── public GET /api/pricing ────────────────────────────────────────────────
async def test_public_pricing_lists_active_only(client, db_session):
    await _seed_plan(db_session, key="after_hours", monthly=23900, sort=0)
    await _seed_plan(db_session, key="growth", monthly=57900, sort=2)
    await _seed_plan(db_session, key="dead", monthly=999, active=False, sort=9)

    r = await client.get("/api/pricing")
    assert r.status_code == 200
    body = r.json()
    keys = [p["key"] for p in body["plans"]]
    assert keys == ["after_hours", "growth"]  # inactive hidden, sorted by sort_order
    # Self-healed settings expose the advertised percents (referrer reward NOT leaked).
    assert body["annual_discount_percent"] == 16
    assert body["invitee_discount_percent"] == 15
    assert "referrer_reward_months" not in body


# ── admin GET /pricing ─────────────────────────────────────────────────────
async def test_admin_get_pricing_includes_inactive(client, db_session):
    await _internal(db_session, clerk_id="fin_p1", role="finance")  # MANAGE_PRICING
    await _seed_plan(db_session, key="after_hours", active=True, sort=0)
    await _seed_plan(db_session, key="dead", active=False, sort=9)
    r = await client.get("/api/admin/pricing", headers=_h("fin_p1"))
    assert r.status_code == 200
    body = r.json()
    assert {p["plan_key"] for p in body["plans"]} == {"after_hours", "dead"}
    assert body["config"]["referrer_reward_months"] == 1


# ── admin PUT /pricing/{key} ───────────────────────────────────────────────
async def test_admin_update_plan_price(client, db_session):
    await _internal(db_session, clerk_id="fin_p2", role="finance")
    await _seed_plan(db_session, key="growth", monthly=57900)
    r = await client.put("/api/admin/pricing/growth", headers=_h("fin_p2"),
                         json={"monthly_cents": 60000, "highlight": True})
    assert r.status_code == 200
    assert r.json()["monthly_cents"] == 60000 and r.json()["highlight"] is True
    # Public read reflects the edit immediately (no deploy).
    pub = await client.get("/api/pricing")
    growth = next(p for p in pub.json()["plans"] if p["key"] == "growth")
    assert growth["monthly_cents"] == 60000


async def test_admin_update_plan_validation(client, db_session):
    await _internal(db_session, clerk_id="fin_p3", role="finance")
    await _seed_plan(db_session, key="after_hours")
    # negative money → 422
    r1 = await client.put("/api/admin/pricing/after_hours", headers=_h("fin_p3"),
                          json={"monthly_cents": -1})
    assert r1.status_code == 422
    # bad reactivation level → 422
    r2 = await client.put("/api/admin/pricing/after_hours", headers=_h("fin_p3"),
                          json={"reactivation_level": "platinum"})
    assert r2.status_code == 422
    # empty payload → 422
    r3 = await client.put("/api/admin/pricing/after_hours", headers=_h("fin_p3"), json={})
    assert r3.status_code == 422
    # unknown plan → 404
    r4 = await client.put("/api/admin/pricing/nope", headers=_h("fin_p3"),
                          json={"monthly_cents": 100})
    assert r4.status_code == 404


# ── admin PUT /pricing-config ──────────────────────────────────────────────
async def test_admin_update_config(client, db_session):
    await _internal(db_session, clerk_id="fin_p4", role="finance")
    r = await client.put("/api/admin/pricing-config", headers=_h("fin_p4"),
                         json={"invitee_discount_percent": 20, "referrer_reward_months": 2})
    assert r.status_code == 200
    assert r.json()["invitee_discount_percent"] == 20
    assert r.json()["referrer_reward_months"] == 2
    # out-of-range percent → 422
    bad = await client.put("/api/admin/pricing-config", headers=_h("fin_p4"),
                           json={"annual_discount_percent": 150})
    assert bad.status_code == 422
    # negative reward months → 422
    neg = await client.put("/api/admin/pricing-config", headers=_h("fin_p4"),
                           json={"referrer_reward_months": -1})
    assert neg.status_code == 422
    # absurdly high reward months → 422 (bounded 0–24, no unlimited-credit foot-gun)
    huge = await client.put("/api/admin/pricing-config", headers=_h("fin_p4"),
                            json={"referrer_reward_months": 999999})
    assert huge.status_code == 422


async def test_settings_singleton_is_stable(client, db_session):
    """Self-heal always resolves to the SAME singleton row — repeated reads/writes
    never spawn a second settings row (fixed-id + ON CONFLICT)."""
    from sqlalchemy import func, select

    from app.models.pricing_plan import PlatformSetting
    await _internal(db_session, clerk_id="fin_p5", role="finance")
    await client.get("/api/pricing")  # self-heal creates it
    await client.put("/api/admin/pricing-config", headers=_h("fin_p5"),
                     json={"invitee_discount_percent": 22})
    await client.get("/api/pricing")
    count = (await db_session.execute(
        select(func.count()).select_from(PlatformSetting)
    )).scalar_one()
    assert count == 1


# ── RBAC ───────────────────────────────────────────────────────────────────
async def test_pricing_denied_wrong_roles(client, db_session):
    await _internal(db_session, clerk_id="sales_p", role="sales")    # no MANAGE_PRICING
    await _internal(db_session, clerk_id="sup_p", role="support")
    for who in ("sales_p", "sup_p", "random_clinic"):
        g = await client.get("/api/admin/pricing", headers=_h(who))
        assert g.status_code in (401, 403), who
        p = await client.put("/api/admin/pricing/after_hours", headers=_h(who),
                             json={"monthly_cents": 1})
        assert p.status_code in (401, 403), who


async def test_super_admin_can_edit(client, db_session):
    await _internal(db_session, clerk_id="sa_p", role="super_admin")
    await _seed_plan(db_session, key="after_hours")
    r = await client.put("/api/admin/pricing/after_hours", headers=_h("sa_p"),
                         json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
