"""ADM8 — subscription cancel/resume: stripe client (mocked HTTP) + admin endpoints."""

from __future__ import annotations

import uuid

import httpx

import app.routes.admin as admin_mod
import app.services.stripe_client as sc
from app.models.dentiva_staff import DentivaStaff
from app.models.subscription import Subscription
from app.models.user import User
from tests.conftest import seed_practice


class _Cfg:
    stripe_secret_key = "sk_test_x"


# ── stripe client ────────────────────────────────────────────────────────────
async def test_cancel_at_period_end_posts_flag(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.content.decode()))
        return httpx.Response(200, json={"id": "sub_1", "cancel_at_period_end": True})

    await sc.cancel_subscription("sub_1", transport=httpx.MockTransport(handler))
    assert calls[0][:2] == ("POST", "/v1/subscriptions/sub_1")
    assert "cancel_at_period_end=true" in calls[0][2]


async def test_cancel_immediately_deletes(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"id": "sub_2", "status": "canceled"})

    await sc.cancel_subscription("sub_2", at_period_end=False,
                                 transport=httpx.MockTransport(handler))
    assert calls == [("DELETE", "/v1/subscriptions/sub_2")]


async def test_resume_clears_flag(monkeypatch):
    monkeypatch.setattr(sc, "get_settings", lambda: _Cfg())
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.content.decode()))
        return httpx.Response(200, json={"id": "sub_3", "cancel_at_period_end": False})

    await sc.resume_subscription("sub_3", transport=httpx.MockTransport(handler))
    assert "cancel_at_period_end=false" in calls[0][2]


# ── admin endpoints ──────────────────────────────────────────────────────────
async def _internal(db_session, *, clerk_id, role):
    u = User(id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
             email=f"{clerk_id}@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role=role))
    await db_session.commit()


def _h(user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": "org_internal"}


async def _sub(db_session, practice_id, *, stripe_id="sub_test", status="active"):
    s = Subscription(id=uuid.uuid4(), practice_id=practice_id,
                     stripe_subscription_id=stripe_id, plan="starter",
                     included_minutes=500, status=status)
    db_session.add(s)
    await db_session.commit()
    return s


def _fake_stripe(monkeypatch):
    calls = []

    async def _cancel(sid, *, at_period_end=True, **_kw):
        calls.append(("cancel", sid, at_period_end))
        return {"id": sid}

    async def _resume(sid, **_kw):
        calls.append(("resume", sid))
        return {"id": sid}

    monkeypatch.setattr(admin_mod, "cancel_subscription", _cancel)
    monkeypatch.setattr(admin_mod, "resume_subscription", _resume)
    return calls


async def test_cancel_at_period_end_then_resume(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_c1", role="finance")
    practice, _ = await seed_practice(db_session, name="CanCo",
                                      clerk_org_id="o_can1", clerk_user_id="u_can1")
    await _sub(db_session, practice.id)
    calls = _fake_stripe(monkeypatch)

    r = await client.post(f"/api/admin/clinics/{practice.id}/subscription/cancel",
                          headers=_h("fin_c1"), json={})
    assert r.status_code == 200
    assert r.json()["cancel_at_period_end"] is True
    assert r.json()["status"] == "active"  # service continues until period end
    assert calls == [("cancel", "sub_test", True)]

    # cancel again while pending → 409
    again = await client.post(f"/api/admin/clinics/{practice.id}/subscription/cancel",
                              headers=_h("fin_c1"), json={})
    assert again.status_code == 409

    # resume clears the flag
    res = await client.post(f"/api/admin/clinics/{practice.id}/subscription/resume",
                            headers=_h("fin_c1"))
    assert res.status_code == 200 and res.json()["cancel_at_period_end"] is False
    # resume with nothing pending → 409
    res2 = await client.post(f"/api/admin/clinics/{practice.id}/subscription/resume",
                             headers=_h("fin_c1"))
    assert res2.status_code == 409


async def test_cancel_immediately(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_c2", role="finance")
    practice, _ = await seed_practice(db_session, name="CanCo2",
                                      clerk_org_id="o_can2", clerk_user_id="u_can2")
    await _sub(db_session, practice.id, stripe_id="sub_now")
    calls = _fake_stripe(monkeypatch)

    r = await client.post(f"/api/admin/clinics/{practice.id}/subscription/cancel",
                          headers=_h("fin_c2"), json={"mode": "immediately"})
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert calls == [("cancel", "sub_now", False)]

    # cancelled is terminal: neither cancel nor resume works
    for path, body in (("cancel", {}), ("resume", None)):
        r2 = await client.post(
            f"/api/admin/clinics/{practice.id}/subscription/{path}",
            headers=_h("fin_c2"), **({"json": body} if body is not None else {}),
        )
        assert r2.status_code == 409, path


async def test_cancel_guards(client, db_session, monkeypatch):
    await _internal(db_session, clerk_id="fin_c3", role="finance")
    practice, _ = await seed_practice(db_session, name="CanCo3",
                                      clerk_org_id="o_can3", clerk_user_id="u_can3")
    _fake_stripe(monkeypatch)

    # no subscription → 404
    r404 = await client.post(f"/api/admin/clinics/{uuid.uuid4()}/subscription/cancel",
                             headers=_h("fin_c3"), json={})
    assert r404.status_code == 404
    # pilot (no stripe id) → 409
    await _sub(db_session, practice.id, stripe_id=None)
    r409 = await client.post(f"/api/admin/clinics/{practice.id}/subscription/cancel",
                             headers=_h("fin_c3"), json={})
    assert r409.status_code == 409
    # bad mode → 422
    r422 = await client.post(f"/api/admin/clinics/{practice.id}/subscription/cancel",
                             headers=_h("fin_c3"), json={"mode": "someday"})
    assert r422.status_code == 422


async def test_cancel_denied_wrong_roles(client, db_session):
    await _internal(db_session, clerk_id="sup_c", role="support")  # no MANAGE_SUBSCRIPTIONS
    for who in ("sup_c", "random_clinic"):
        r = await client.post(f"/api/admin/clinics/{uuid.uuid4()}/subscription/cancel",
                              headers=_h(who), json={})
        assert r.status_code in (401, 403), who


async def test_webhook_syncs_pending_cancel_flag(db_session):
    """subscription.updated carrying cancel_at_period_end must sync the flag, and
    its ABSENCE must not clear a scheduled cancellation."""
    from app.webhooks.stripe import _handle_subscription_updated

    practice, _ = await seed_practice(db_session, name="CanCo4",
                                      clerk_org_id="o_can4", clerk_user_id="u_can4")
    sub = await _sub(db_session, practice.id, stripe_id="sub_wh")

    await _handle_subscription_updated(db_session, {
        "object": "subscription", "id": "sub_wh", "status": "active",
        "cancel_at_period_end": True,
    })
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.cancel_at_period_end is True

    # Event without the key → flag untouched.
    await _handle_subscription_updated(db_session, {
        "object": "subscription", "id": "sub_wh", "status": "active",
    })
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.cancel_at_period_end is True


async def test_webhook_skips_stale_out_of_order_event(db_session):
    """Reviewer #2: a redelivered OLD event (cancel) arriving after a newer one
    (resume) must be skipped — not roll cancel_at_period_end back to true."""
    from app.webhooks.stripe import _handle_subscription_updated

    practice, _ = await seed_practice(db_session, name="CanCo6",
                                      clerk_org_id="o_can6", clerk_user_id="u_can6")
    sub = await _sub(db_session, practice.id, stripe_id="sub_ooo")

    # Newer event first (resume at t=2000).
    r1 = await _handle_subscription_updated(db_session, {
        "object": "subscription", "id": "sub_ooo", "status": "active",
        "cancel_at_period_end": False, "_event_created": 2000,
    })
    assert r1.startswith("synced")
    # Stale retry of the older cancel event (t=1000) → skipped entirely.
    r2 = await _handle_subscription_updated(db_session, {
        "object": "subscription", "id": "sub_ooo", "status": "past_due",
        "cancel_at_period_end": True, "_event_created": 1000,
    })
    assert r2 == "stale_event_skipped"
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.cancel_at_period_end is False   # not rolled back
    assert sub.status == "active"              # status not rolled back either
    assert sub.last_stripe_event_ts == 2000

    # Genuinely newer event still applies.
    r3 = await _handle_subscription_updated(db_session, {
        "object": "subscription", "id": "sub_ooo", "status": "active",
        "cancel_at_period_end": True, "_event_created": 3000,
    })
    assert r3.startswith("synced")
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.cancel_at_period_end is True and sub.last_stripe_event_ts == 3000


async def test_deleted_webhook_keeps_cancelled_terminal(db_session):
    """suspend_practice (fired by subscription.deleted) must not relabel a
    'cancelled' subscription as 'suspended'."""
    from app.services.billing_service import suspend_practice

    practice, _ = await seed_practice(db_session, name="CanCo5",
                                      clerk_org_id="o_can5", clerk_user_id="u_can5")
    sub = await _sub(db_session, practice.id, stripe_id="sub_term", status="cancelled")
    await suspend_practice(db_session, practice)
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.status == "cancelled"
    assert practice.status == "suspended"
