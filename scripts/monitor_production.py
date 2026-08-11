#!/usr/bin/env python3
"""Ask production whether it is still the thing we think it is.

Runs OUTSIDE the application, from a scheduled job. That is the whole point: a
check living inside the process it is checking proves nothing when the process is
the thing that is wrong. /health can answer cheerfully from a container whose
database is gone, whose deploy rolled back a week ago, or whose webhook secret
was dropped — all three have happened here.

What it deliberately does NOT do: create a patient, a booking, or a callback.
Every write path is exercised against a canary practice or not at all. A
synthetic appointment in a live clinic's calendar is worse than no monitoring —
somebody has to explain it to a receptionist who did not book it.

So these checks are observational or negative. They read what production says
about itself, and they poke one endpoint expecting to be refused.

Usage:
  python scripts/monitor_production.py                 # exit 1 on any failure
  python scripts/monitor_production.py --expect-sha abc1234
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "https://dentiva-backend-production.up.railway.app"
TIMEOUT = 20

# Alerts that mean a promise made to a patient was not kept. These are worth
# waking someone for; the rest are worth a dashboard.
_PROMISE_KINDS = ("page_not_delivered", "pms_write_", "pms_cancel_", "pms_move_")

# What the forgery probe posts. It goes to a LIVE write endpoint, so if the
# signature check ever regressed this is the payload that would be accepted —
# which is why it names no tool, no patient and no clinic. A probe carrying a
# real booking would, on the one day the guard failed, become a real booking.
_PROBE_BODY = {"event": "call_started", "call_id": "monitor-probe"}


class Failure(Exception):
    """A check that did not pass. The message is what a human will read first."""


def _get(path: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"{BASE}{path}", headers={"User-Agent": "dentovox-monitor"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001 — unreachable IS the finding
        raise Failure(f"{path} unreachable: {type(exc).__name__}: {exc}") from exc


def check_alive() -> str:
    status, body = _get("/health")
    if status != 200 or body.get("status") != "ok":
        raise Failure(f"/health answered {status} {body}")
    return "answering"


def check_detailed(expect_sha: str | None) -> str:
    status, body = _get("/health/detailed")
    # 503 is how this endpoint reports degraded, so read the body either way.
    if status not in (200, 503):
        raise Failure(f"/health/detailed answered {status}")
    if body.get("db") != "ok":
        raise Failure("the database is unreachable from production")
    if body.get("rls_enforced") is not True:
        raise Failure(
            "rls_enforced is not true — clinic isolation is OFF. Production is "
            "connecting as a role that bypasses row-level security, which is how "
            "one clinic's patients become visible to another."
        )
    if not body.get("webhook_verified"):
        raise Failure(
            "RETELL_WEBHOOK_SECRET is unset — anyone who knows the URL can book, "
            "cancel and read on behalf of a clinic."
        )
    revision = body.get("revision", "absent")
    if expect_sha and revision != expect_sha:
        raise Failure(
            f"production is running {revision}, not {expect_sha}. Either a deploy "
            "rolled back or one never landed; everything merged since is not in "
            "front of a patient."
        )
    broken = {
        kind: count
        for kind, count in (body.get("alerts", {}).get("by_kind") or {}).items()
        if kind.startswith(_PROMISE_KINDS)
    }
    if broken:
        raise Failure(
            f"promises to patients were not kept in the last hour: {broken}. "
            "These are the alerts raised when a page never sent or a booking "
            "never reached the clinic's calendar."
        )
    recent = (body.get("alerts") or {}).get("count_last_hour", 0)
    return f"revision {revision}, rls enforced, {recent} alerts"


def check_webhook_refuses_forgeries() -> str:
    """The one write endpoint reachable from the internet, poked with no
    signature. A 200 here means anybody can book and cancel for any clinic."""
    request = urllib.request.Request(
        f"{BASE}/webhooks/retell",
        data=json.dumps(_PROBE_BODY).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "dentovox-monitor"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raise Failure(
                f"an unsigned webhook was ACCEPTED ({response.status}). Anyone who "
                "knows the URL can book, cancel and read patient data."
            )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return f"unsigned request refused ({exc.code})"
        raise Failure(f"unsigned webhook answered {exc.code}, expected 401") from exc
    except Failure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Failure(f"webhook probe failed: {type(exc).__name__}: {exc}") from exc


CHECKS = (
    ("production answers", lambda args: check_alive()),
    ("database, isolation, deploy", lambda args: check_detailed(args.expect_sha)),
    ("forged webhooks refused", lambda args: check_webhook_refuses_forgeries()),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expect-sha", default=None,
        help="short commit production should be running; skipped when absent",
    )
    args = parser.parse_args()

    failures = []
    for name, check in CHECKS:
        try:
            print(f"  ok    {name}: {check(args)}")
        except Failure as exc:
            print(f"  FAIL  {name}: {exc}")
            failures.append((name, str(exc)))

    if not failures:
        print("\nProduction is healthy.")
        return 0
    print(f"\n{len(failures)} check(s) failed:\n")
    for name, message in failures:
        print(f"  {name}\n    {message}\n")
    print(
        "None of these are flaky by construction — every one reads a fact "
        "production reports about itself. Treat a failure as real."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
