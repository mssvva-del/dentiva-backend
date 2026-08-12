"""Operational alerts + /health/detailed."""

from __future__ import annotations

from app.observability import alerts


def test_record_and_summarize_alerts(monkeypatch):
    alerts._RECENT.clear()
    t = 1_000_000.0
    alerts.record_alert("twilio_send_failed", "status=400", now=t)
    alerts.record_alert("twilio_send_failed", "status=401", now=t + 1)
    alerts.record_alert("web_call_failed", "retell_status=404", now=t + 2)

    s = alerts.recent_alerts(now=t + 10)
    assert s["count_last_hour"] == 3
    assert s["by_kind"] == {"twilio_send_failed": 2, "web_call_failed": 1}
    assert s["last_kind"] == "web_call_failed"
    assert s["last_detail"] == "retell_status=404"  # code surfaced for diagnosis


def test_alerts_age_out_after_an_hour():
    alerts._RECENT.clear()
    t = 2_000_000.0
    alerts.record_alert("old", "x", now=t)
    # 61 minutes later → the old alert has aged out of the 1h window.
    s = alerts.recent_alerts(now=t + 3_660)
    assert s["count_last_hour"] == 0


def test_never_leaks_full_detail_length():
    alerts._RECENT.clear()
    alerts.record_alert("k", "Z" * 999, now=5.0)
    _, _, detail = alerts._RECENT[-1]
    assert len(detail) <= 200


async def test_health_detailed_ok(client):
    alerts._RECENT.clear()
    r = await client.get("/health/detailed")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["alerts"]["count_last_hour"] == 0
    # Config flags present (values depend on env; just assert the keys exist).
    for k in ("voice_configured", "sms_enabled", "webhook_verified"):
        assert k in body


async def test_health_detailed_degraded_when_alerts_fire(client):
    alerts._RECENT.clear()
    alerts.record_alert("twilio_send_failed", "status=500")
    r = await client.get("/health/detailed")
    # Alerts firing → 503 so an uptime monitor pages, even with the DB up.
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["alerts"]["count_last_hour"] >= 1
    alerts._RECENT.clear()


async def test_health_reports_whether_rls_is_actually_enforced(client):
    """RLS FORCE is the tenant boundary, and a SUPERUSER/BYPASSRLS login role is
    exempt from every policy — silently, with nothing failing. The pilot escape
    hatch (ALLOW_SUPERUSER_DB) makes that state bootable, so health has to keep
    saying which state we are in, not just check it once at startup."""
    r = await client.get("/health/detailed")
    body = r.json()
    assert "rls_enforced" in body
    # Tests connect as dentiva_app (NOSUPERUSER NOBYPASSRLS) — the same role
    # production is supposed to use.
    assert body["rls_enforced"] is True


async def test_capabilities_report_never_leaks_a_value(client):
    """This block sits on an unauthenticated endpoint, which is the whole point —
    "what are we still missing?" should be answerable without a login. Booleans
    only: a key prefix or an "sk_live_…" hint here would put a credential on a
    public URL."""
    caps = (await client.get("/health/detailed")).json()["capabilities"]
    assert caps and all(isinstance(v, bool) for v in caps.values())


async def test_the_features_that_fail_quietly_are_listed(client):
    """Each of these is a whole feature that goes inert rather than loud when its
    credential is absent: no subscription can be started, no reactivation call
    dials, no PMS answers. Missing access looks exactly like a quiet system."""
    caps = (await client.get("/health/detailed")).json()["capabilities"]
    for name in ("take_payment", "billing_webhook", "identity_webhook",
                 "outbound_calls", "pms_open_dental", "call_review"):
        assert name in caps


async def test_health_names_the_commit_it_is_running(client):
    """Nine merged pull requests once sat in main for a week while production
    served a build from before any of them, two of them security fixes. Every
    merge had been followed by a health check returning "ok" — which means the
    box is alive, not that the box is current.

    With no revision in the response the only way to notice was for a change to
    happen to be visible from outside. That is luck, not verification."""
    body = (await client.get("/health/detailed")).json()
    assert "revision" in body
    assert isinstance(body["revision"], str) and body["revision"]


async def test_shared_pms_credentials_only_warn_when_there_are_credentials(
    client, db_session, monkeypatch
):
    """The warning exists for the state where a second clinic reads the first
    clinic's calendar. With no bridge configured there is nothing to read, and
    firing on the harmless version teaches everyone to ignore it — which is how
    the dangerous version arrives unread.

    It fired for a day in production before this narrowed it, on an installation
    with no PMS credentials at all.
    """
    from app.adapters import bridge
    from tests.conftest import seed_practice

    for i in (1, 2):
        practice, _ = await seed_practice(
            db_session, name=f"PMS Clinic {i}",
            clerk_org_id=f"org_pms{i}", clerk_user_id=f"user_pms{i}",
        )
        practice.pms_system = "open_dental"
    await db_session.commit()

    monkeypatch.setattr(bridge, "_kolla_configured", lambda: False)
    monkeypatch.setattr(bridge, "_nexhealth_configured", lambda: False)
    body = (await client.get("/health/detailed")).json()
    assert body["pms_credentials_ambiguous"] is False, (
        "warned about sharing credentials that do not exist"
    )

    monkeypatch.setattr(bridge, "_nexhealth_configured", lambda: True)
    body = (await client.get("/health/detailed")).json()
    assert body["pms_credentials_ambiguous"] is True, (
        "two clinics, one set of credentials, and nobody said so"
    )


async def test_an_alert_outlives_the_process_that_raised_it(db_session):
    """The buffer is per-process and Railway redeploys on every merge, so the
    window in which page_not_delivered_urgent_callback can vanish was opened
    several times a day. With two instances the health endpoint showed whichever
    half the check landed on."""
    from app.observability.alerts import _write, recent_alerts_stored

    alerts._RECENT.clear()
    await _write("page_not_delivered_urgent_callback", "reason=sms_disabled")

    # A fresh process: nothing in memory, and the alert still has to be there.
    alerts._RECENT.clear()
    stored = await recent_alerts_stored()
    assert stored is not None
    assert stored["by_kind"].get("page_not_delivered_urgent_callback") == 1
    assert stored["last_detail"] == "reason=sms_disabled"


async def test_an_unreadable_store_falls_back_rather_than_reporting_quiet(
    monkeypatch,
):
    """None means "use the buffer", not "no alerts". Reporting quiet because the
    database is unreachable is the failure this file exists to prevent,
    reproduced one level up."""
    from app.observability.alerts import recent_alerts_stored

    class _Broken:
        def __call__(self):
            raise RuntimeError("database is gone")

    monkeypatch.setattr("app.db.platform_session_factory", _Broken())
    assert await recent_alerts_stored() is None


async def test_the_health_endpoint_prefers_what_was_stored(client, db_session):
    from app.observability.alerts import _write

    alerts._RECENT.clear()
    await _write("pms_write_conflict", "booking=probe")

    body = (await client.get("/health/detailed")).json()
    assert body["alerts"]["by_kind"].get("pms_write_conflict") == 1, (
        "the endpoint read the empty buffer instead of the stored alert"
    )
