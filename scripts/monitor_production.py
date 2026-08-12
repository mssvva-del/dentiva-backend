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
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

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

# The number the canary answers on. Routing matches it exactly, which is what
# keeps a synthetic call off a real clinic — the fallback it would otherwise hit
# picks the only practice in the database, and that practice has patients.
CANARY_NUMBER = "+10000000000"

# The synthetic caller. Also in the reserved +1000 block, and for the same
# reason: the booking and waitlist paths text the patient, so a probe using a
# plausible-looking number had production trying to SMS it every ten minutes and
# raising twilio_send_failed when it could not. Production reported "degraded"
# around the clock over messages nobody was meant to receive — which is how a
# real alert arrives into a silence everyone has learned to ignore.
PROBE_CALLER = "+10000000001"


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
    if body.get("pms_credentials_ambiguous"):
        raise Failure(
            "more than one clinic has a PMS while the environment's credentials "
            "belong to no named practice. In that state the second clinic reads "
            "the FIRST clinic's calendar, and with writes on books patients into "
            "its chairs. Set PMS_ENV_PRACTICE_ID, or give each practice its own "
            "credentials."
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


def _signed_post(body: dict, secret: str) -> tuple[int, dict]:
    """Speak Retell's own signing scheme: HMAC over the raw body ++ the timestamp."""
    raw = json.dumps(body).encode()
    stamp = int(time.time() * 1000)
    digest = hmac.new(secret.encode(), raw + str(stamp).encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"{BASE}/webhooks/retell",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "dentovox-monitor",
            # The shape our verifier parses: v=<epoch ms>,d=<hex digest>. Copied
            # from the regex rather than from memory of the vendor's docs — the
            # two have differed before.
            "X-Retell-Signature": f"v={stamp},d={digest}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200]
        if exc.code == 401:
            # Name WHICH secret we are holding, so "the signature is wrong" and
            # "the secret is wrong" stop looking identical. A short digest of a
            # 32-character secret gives away nothing usable, and it turns a
            # guessing game into a comparison — the value in the deployment can
            # be fingerprinted the same way.
            raise Failure(
                f"production rejected a correctly-formed signature. The monitor "
                f"holds a secret of {len(secret)} characters, fingerprint "
                f"{hashlib.sha256(secret.encode()).hexdigest()[:8]}. If that does "
                f"not match the deployment's, the wrong value was copied. "
                f"Response: {detail!r}"
            ) from exc
        raise Failure(f"signed webhook rejected with {exc.code}: {detail!r}") from exc
    except Exception as exc:  # noqa: BLE001
        raise Failure(f"signed webhook failed: {type(exc).__name__}: {exc}") from exc


def check_a_call_still_ends_in_a_booking(secret: str | None) -> str:
    """The only check that answers the question the product is sold on.

    Everything else here reads what production says about itself. This one makes
    a call happen and walks every promise the agent makes to a patient: book,
    move, join the waitlist, escalate something urgent, and cancel — the last so
    the canary's calendar does not fill with a decade of synthetic Tuesdays.

    Each of these fails quietly in its own way. A booking that does not happen is
    at least visible in the answer; a waitlist that writes nothing still sounds
    polite, a move can store one date and speak another, and an urgent callback
    is promised to the caller before anything is written.

    Every request carries the canary's number, so it lands on the monitoring
    clinic and nowhere else — and that clinic has no PMS, so nothing it books can
    reach a real calendar even if the routing were wrong.
    """
    if not secret:
        raise Failure(
            "RETELL_WEBHOOK_SECRET is not available to the monitor, so the "
            "booking flow was NOT exercised. Everything above passed and the "
            "thing customers pay for is unchecked — that is worth saying rather "
            "than skipping quietly."
        )

    call_id = f"monitor-{uuid.uuid4().hex[:10]}"
    _signed_post({
        "event": "call_started", "call_id": call_id,
        "call": {"call_id": call_id, "from_number": PROBE_CALLER,
                 "to_number": CANARY_NUMBER,
                 "start_timestamp": int(time.time() * 1000)},
    }, secret)

    _, booked = _signed_post({
        "event": "function_call", "call_id": call_id,
        "call": {"call_id": call_id, "to_number": CANARY_NUMBER},
        "function_name": "book_appointment",
        "args": {
            "patient_first_name": "Monitor", "patient_last_name": "Probe",
            "patient_phone": PROBE_CALLER, "procedure": "cleaning",
            "preferred_date": "2099-11-10", "preferred_time_window": "morning",
        },
    }, secret)
    if not booked.get("booked"):
        raise Failure(
            f"a call did not end in a booking: {json.dumps(booked)[:300]}. This is "
            "the product. Everything else can be green while this is broken."
        )

    # Moving it. The clinic hears a different sentence from the one it stores if
    # this breaks, and the patient arrives on a day nobody expects them — a bug
    # we have already shipped once, on this exact path.
    _, moved = _signed_post({
        "event": "function_call", "call_id": call_id,
        "call": {"call_id": call_id, "to_number": CANARY_NUMBER},
        "function_name": "reschedule_appointment",
        "args": {"patient_phone": PROBE_CALLER, "new_date": "2099-11-17"},
    }, secret)
    if not moved.get("rescheduled"):
        raise Failure(
            f"a booking could not be moved: {json.dumps(moved)[:300]}. Every "
            "patient who calls back to change a time meets this."
        )
    spoken = (moved.get("appointment") or {}).get("date")
    if spoken and spoken not in (moved.get("message") or ""):
        raise Failure(
            f"the agent was told to say {spoken!r} and its sentence does not "
            f"contain it: {moved.get('message')!r}. A patient told one date and "
            "booked for another arrives at a practice not expecting them."
        )

    _, cancelled = _signed_post({
        "event": "function_call", "call_id": call_id,
        "call": {"call_id": call_id, "to_number": CANARY_NUMBER},
        "function_name": "cancel_appointment",
        "args": {"patient_phone": PROBE_CALLER, "reason": "monitoring probe"},
    }, secret)
    if not cancelled.get("cancelled"):
        # Not fatal: the booking worked, which is the headline. But an
        # accumulating calendar eventually makes every later probe fail to find a
        # slot, and that failure would look like a booking bug.
        return "booked and moved; the probe cancellation did not take — canary calendar will fill"

    # The waitlist is what catches demand we cannot serve. It broke once and
    # nothing noticed, because it fails by writing nothing and saying so politely.
    _, waitlisted = _signed_post({
        "event": "function_call", "call_id": call_id,
        "call": {"call_id": call_id, "to_number": CANARY_NUMBER},
        "function_name": "join_waitlist",
        "args": {
            "patient_first_name": "Monitor", "patient_last_name": "Probe",
            "patient_phone": PROBE_CALLER, "procedure": "cleaning",
            "preferred_date": "2099-12-01", "preferred_time_window": "morning",
        },
    }, secret)
    if not waitlisted.get("added"):
        raise Failure(
            f"the waitlist refused a caller: {json.dumps(waitlisted)[:300]}. This "
            "is how a clinic learns somebody wanted an appointment it could not "
            "offer, and it fails by writing nothing while sounding polite."
        )

    # And the one that is not about money. An urgent callback has to be recorded
    # AND paged; the agent has already told the caller the team knows.
    _, urgent = _signed_post({
        "event": "function_call", "call_id": call_id,
        "call": {"call_id": call_id, "to_number": CANARY_NUMBER},
        "function_name": "create_callback_request",
        "args": {
            "patient_name": "Monitor Probe", "patient_phone": PROBE_CALLER,
            "reason": "monitoring probe — synthetic, no patient", "urgent": True,
        },
    }, secret)
    if urgent.get("status") not in ("callback_logged", "er_referral"):
        raise Failure(
            f"an urgent callback was not recorded: {json.dumps(urgent)[:300]}. The "
            "agent tells the caller the team has been notified before this "
            "returns, so a failure here is a promise already made."
        )
    return "booked, moved, waitlisted, escalated and cancelled on the canary"


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
    ("a call still ends in a booking",
     lambda args: check_a_call_still_ends_in_a_booking(os.environ.get("RETELL_WEBHOOK_SECRET"))),
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
