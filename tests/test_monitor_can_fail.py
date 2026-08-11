"""The monitor has to be able to say no.

A monitor that cannot fail is worse than no monitor: it produces a green tick
every ten minutes and a belief that somebody is watching. Every check here is
therefore exercised against a response that should fail it, not only against a
healthy one.

The checks run against production from a scheduled job, so nothing about them is
covered by the rest of the suite. These tests cover the judgement — given this
body, does it complain? — which is the only part that lives in this repository.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "monitor", Path(__file__).resolve().parents[1] / "scripts" / "monitor_production.py"
)
monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor)

_HEALTHY = {
    "status": "ok", "db": "ok", "revision": "abc1234",
    "rls_enforced": True, "webhook_verified": True,
    "alerts": {"count_last_hour": 0, "by_kind": {}},
}


def _answers(body: dict, status: int = 200):
    return lambda path: (status, body)


def test_a_healthy_production_passes(monkeypatch):
    monkeypatch.setattr(monitor, "_get", _answers(_HEALTHY))
    assert "abc1234" in monitor.check_detailed("abc1234")


def test_isolation_switched_off_fails(monkeypatch):
    """The single most dangerous state this system can be in, and it is invisible
    from a call: everything works, and one clinic can read another's patients."""
    monkeypatch.setattr(monitor, "_get", _answers({**_HEALTHY, "rls_enforced": False}))
    with pytest.raises(monitor.Failure, match="isolation is OFF"):
        monitor.check_detailed(None)


def test_a_missing_webhook_secret_fails(monkeypatch):
    """Without it anyone who knows the URL can book and cancel for any clinic."""
    monkeypatch.setattr(monitor, "_get", _answers({**_HEALTHY, "webhook_verified": False}))
    with pytest.raises(monitor.Failure, match="WEBHOOK_SECRET"):
        monitor.check_detailed(None)


def test_a_stale_deploy_fails(monkeypatch):
    """Production served an eleven-day-old build while every health check said
    ok. This is the check that would have caught it on the first morning."""
    monkeypatch.setattr(monitor, "_get", _answers(_HEALTHY))
    with pytest.raises(monitor.Failure, match="rolled back or one never landed"):
        monitor.check_detailed("9999999")


def test_a_broken_promise_fails(monkeypatch):
    """A page that never reached the clinic, or a booking that never reached its
    calendar. The call sounded fine; these alerts are the only trace."""
    monkeypatch.setattr(monitor, "_get", _answers({
        **_HEALTHY,
        "alerts": {"count_last_hour": 1, "by_kind": {"page_not_delivered_urgent_callback": 1}},
    }))
    with pytest.raises(monitor.Failure, match="promises to patients"):
        monitor.check_detailed(None)


def test_ordinary_alerts_do_not_page(monkeypatch):
    """A Twilio hiccup is worth a dashboard, not a 3am email. A monitor that
    cries wolf gets muted, and then it is not a monitor."""
    monkeypatch.setattr(monitor, "_get", _answers({
        **_HEALTHY,
        "alerts": {"count_last_hour": 2, "by_kind": {"twilio_send_failed": 2}},
    }))
    assert monitor.check_detailed(None)


def test_a_degraded_body_is_still_read(monkeypatch):
    """/health/detailed answers 503 when degraded. Treating the status code as
    the whole answer would throw away the reason."""
    monkeypatch.setattr(monitor, "_get", _answers({**_HEALTHY, "db": "down"}, status=503))
    with pytest.raises(monitor.Failure, match="database is unreachable"):
        monitor.check_detailed(None)


def test_an_unreachable_production_fails(monkeypatch):
    def _explode(path):
        raise monitor.Failure(f"{path} unreachable: TimeoutError")

    monkeypatch.setattr(monitor, "_get", _explode)
    with pytest.raises(monitor.Failure, match="unreachable"):
        monitor.check_alive()


def test_the_probe_body_never_carries_anything_real():
    """The forgery probe posts to a live write endpoint. If the signature check
    ever regressed, this body is what production would accept — so it names no
    tool, no patient and no clinic. A probe carrying a real booking would, on the
    one day the guard failed, become a real booking."""
    assert monitor._PROBE_BODY == {"event": "call_started", "call_id": "monitor-probe"}
    assert "function_name" not in monitor._PROBE_BODY


def test_the_booking_probe_always_targets_the_canary():
    """The one check that WRITES. If it ever addressed anything else, a synthetic
    patient would appear in a real clinic's calendar every ten minutes — and the
    routing fallback it would hit picks the only practice in the database, which
    is a practice with patients."""
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "monitor_production.py"
    ).read_text()
    start = source.index("def check_a_call_still_ends_in_a_booking")
    end = source.index("def check_webhook_refuses_forgeries")
    probe = source[start:end]
    assert probe.count("CANARY_NUMBER") >= 3, "a request went out without the canary number"
    assert "to_number" in probe


def test_a_missing_secret_fails_rather_than_skips(monkeypatch):
    """Everything else can pass while the product is broken. A quiet skip here
    would produce a green run that means nothing."""
    with pytest.raises(monitor.Failure, match="NOT exercised"):
        monitor.check_a_call_still_ends_in_a_booking(None)


def test_the_canary_number_matches_what_the_backend_provisions():
    """Two constants, two files, one meaning. If they drift, the probe routes by
    the fallback into a real clinic — the exact failure this design exists to
    make impossible."""
    from app.services.canary import CANARY_NUMBER as backend_number

    assert monitor.CANARY_NUMBER == backend_number


def test_a_rejected_signature_names_the_secret_it_used():
    """"The signature is wrong" and "the secret is wrong" look identical from a
    401, and only one of them is a code bug. A short digest turns the guessing
    game into a comparison; it gives away nothing usable about a 32-character
    secret."""
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "monitor_production.py"
    ).read_text()
    assert "fingerprint" in source
    # The secret itself must never reach a log. Only its length and a digest.
    start = source.index("if exc.code == 401:")
    block = source[start:start + 900]
    assert "sha256" in block
    assert "{secret}" not in block
