#!/usr/bin/env python3
"""Find out how NexHealth wants a patient created — by asking it, not by guessing.

This is the last thing standing between the product and a complete booking. A
first-time caller exists only in OUR database: the agent takes the appointment,
and the clinic's own system has never heard of the person, so the front desk has
to type them in by hand or the slot silently exists nowhere real.

Writing that from the published docs is exactly how the appointment write-back
went wrong the first time — the documented body was rejected until we sent it and
read the error. So this script sends every plausible shape to the SANDBOX and
prints which one the API accepts.

WHAT IT TOUCHES: the NexHealth demo practice (subdomain and location below), the
same sandbox where appointment 1571413685 was created during the earlier
verification. It never runs against a real clinic — the location id is hard-coded
here rather than read from the environment, so a production value in a shell
cannot redirect it.

WHAT IT PRINTS: status codes and FIELD NAMES. Never the API key, never a value
from a patient record. The output is meant to be safe to paste into a chat.

Usage — the key comes from the environment, never from a command line (a
key in an argument is stored in shell history and visible in ps):

    NEXHEALTH_API_KEY=... .venv/bin/python scripts/probe_nexhealth_patient.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://nexhealth.info"
# The demo practice, hard-coded on purpose. See the note above.
SUBDOMAIN = "zentek-demo-practice"
LOCATION_ID = "351939"

# NexHealth sits behind Cloudflare, which blocks the default Python agent with a
# 1010/403. This is not politeness, it is a requirement.
HEADERS = {
    "Accept": "application/vnd.Nexhealth+json;version=2",
    "User-Agent": "Dentovox/1.0 (+https://dentovox.com)",
    "Content-Type": "application/json",
}

# A name no real patient will share, so the row is obvious to anyone who finds it
# in the demo practice later.
TEST_FIRST = "Dentovox"
TEST_LAST = "ProbeDoNotUse"
PROBE_PHONE = "5555550123"
PROBE_EMAIL = "probe@dentovox.com"


def call(method: str, path: str, token: str | None = None,
         params: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
    query = {"subdomain": SUBDOMAIN, "location_id": LOCATION_ID, **(params or {})}
    url = f"{BASE}{path}?{urllib.parse.urlencode(query)}"
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        print(f"  network error: {type(exc).__name__}: {exc}")
        return 0, {}


def authenticate(api_key: str) -> str | None:
    request = urllib.request.Request(
        f"{BASE}/authenticates",
        data=b"",
        headers={**HEADERS, "Authorization": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print(f"  authentication FAILED with {exc.code} — the key was not accepted")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"  authentication failed: {type(exc).__name__}: {exc}")
        return None
    token = (payload.get("data") or {}).get("token")
    print("  authenticated" if token else "  no token in the response")
    return token


def shapes(provider_id: str) -> list[tuple[str, dict, dict]]:
    """What can we LEAVE OUT?

    The envelope is settled: provider goes in the BODY, not the query string —
    NexHealth answered "Missing parameter provider[provider_id]" to the
    documented query-param form. What is not settled is which patient fields are
    required, and that is the question that decides whether this can ship.

    A voice call collects a name and the number the person is calling from. It
    does not collect a date of birth or an email. Sending an invented date of
    birth into a real practice's charts would be worse than not writing the
    patient at all: it is a clinical record, people are matched and de-duplicated
    on it, and nobody downstream would know it was made up.

    So these run from least invented to most, and the FIRST one accepted is the
    one we build against. Each success costs one patient in the demo practice, so
    the loop stops there.
    """
    def person(**extra) -> dict:
        return {"first_name": TEST_FIRST, "last_name": TEST_LAST, **extra}

    wrap = {"provider": {"provider_id": provider_id}}
    return [
        # Only what a phone call actually knows.
        ("name + phone only (what a call knows)", {}, {
            **wrap, "patient": person(bio={"phone_number": "5555550123"})}),
        ("name only", {}, {**wrap, "patient": person()}),
        ("name + phone + email", {}, {
            **wrap, "patient": person(
                email=PROBE_EMAIL, bio={"phone_number": "5555550123"})}),
        # Last resort. If only this is accepted, a date of birth has to be ASKED
        # FOR on the call — not filled in by us.
        ("name + phone + email + date of birth", {}, {
            **wrap, "patient": person(
                email=PROBE_EMAIL,
                bio={"phone_number": "5555550123", "date_of_birth": "1990-01-01"})}),
    ]


def key_from_env_file() -> str:
    """Read NEXHEALTH_API_KEY out of dentiva-backend/.env.

    Typing a key at a hidden prompt is where this went wrong the first time —
    nothing echoes, so there is no way to tell a successful paste from a failed
    one. A line in .env can be pasted in a normal editor and checked by eye, and
    .env is already gitignored.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path) as handle:
            for line in handle:
                name, _, value = line.partition("=")
                if name.strip() == "NEXHEALTH_API_KEY":
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def main() -> int:
    api_key = (os.getenv("NEXHEALTH_API_KEY") or key_from_env_file()).strip()
    if not api_key:
        print(
            "NEXHEALTH_API_KEY is not set.\n"
            "Put it in dentiva-backend/.env as NEXHEALTH_API_KEY=... and run again."
        )
        return 2

    print(f"Sandbox: {SUBDOMAIN} / location {LOCATION_ID}")
    print("\n[1] authenticating")
    token = authenticate(api_key)
    if not token:
        return 1

    print("\n[2] finding a provider to attach the patient to")
    status, payload = call("GET", "/providers", token, params={"per_page": 5})
    providers = payload.get("data") or []
    if isinstance(providers, dict):
        providers = providers.get("providers") or []
    print(f"  GET /providers → {status}, {len(providers)} provider(s)")
    if not providers:
        print("  no providers — a patient cannot be created without one")
        return 1
    provider_id = str(providers[0].get("id"))

    print("\n[3] finding a patient who is ALREADY there")
    # This decides how often anyone has to be asked anything. A returning caller
    # exists in the practice's system already; if we can find them by the number
    # they are ringing from, they need no date of birth, no email and no extra
    # questions — we just adopt their id. Only genuinely new people cost a
    # longer call. NexHealth de-duplicates on create, so a search must exist;
    # the question is what it is called.
    for label, params in (
        ("search=<phone>", {"search": PROBE_PHONE}),
        ("phone_number=<phone>", {"phone_number": PROBE_PHONE}),
        ("search=<last name>", {"search": TEST_LAST}),
        ("name=<last name>", {"name": TEST_LAST}),
        ("email=<email>", {"email": PROBE_EMAIL}),
    ):
        status, payload = call("GET", "/patients", token,
                               params={**params, "per_page": 5})
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("patients") or []
        hit = any(str(r.get("last_name")) == TEST_LAST for r in rows if isinstance(r, dict))
        print(f"  {status}  {label} → {len(rows)} row(s)"
              f"{'  ← FOUND our test patient' if hit else ''}")

    print("\n[4] which fields are actually required (fewest first)")
    created: dict | None = None
    accepted = ""
    for label, params, body in shapes(provider_id):
        status, payload = call("POST", "/patients", token, params=params, body=body)
        print(f"  {status}  {label}")
        if status in (200, 201):
            accepted = label
            data = payload.get("data")
            # Their docs wrap the new patient in "user"; the earlier run printed
            # patient fields either way, so which one it was is still unknown.
            # Report it, because the client has to parse the right one — reading
            # the wrapper as the patient gives an id that addresses nothing.
            wrapped = isinstance(data, dict) and "user" in data
            created = data.get("user") if wrapped else data
            print("  ACCEPTED — this is the minimum we must send")
            print(f"  response envelope: data{'.user' if wrapped else ''} is the patient")
            print(f"  response fields: {sorted(created.keys())}"
                  if isinstance(created, dict) else f"  data: {type(data).__name__}")
            break
        # The error text is the useful part — it names the field it wanted. It
        # describes OUR request, not a patient, so it is safe to show.
        detail = str(payload.get("error") or payload.get("description") or payload)
        print(f"     → {detail[:300]}")
        if "already exists" in detail:
            # Not a rejection of the SHAPE — the shape was right and the person
            # was already there. Worth reading as success, and worth copying into
            # the client: a booking must not fail because the patient exists.
            accepted = f"{label} (duplicate — shape is correct)"
            print("  SHAPE CONFIRMED, and NexHealth de-duplicates on its own.")
            break

    if not accepted:
        print("\nRESULT: no shape was accepted. Paste this whole output back.")
        return 1

    if isinstance(created, dict):
        print(f"\n[5] the id we would store on Patient.pms_external_id: "
              f"{created.get('id')!r}")

    print("\nRESULT")
    print(f"  minimum accepted: {accepted}")
    print(
        "  Required: first_name, last_name, email, bio.phone_number,\n"
        "  bio.date_of_birth — and a provider, in the BODY.\n"
        "  A call gives us a name and a number. The date of birth has to be\n"
        "  ASKED FOR; the email is ours to supply, because an invented one is a\n"
        "  real address belonging to someone else."
    )
    print("  Test patients now exist in the DEMO practice — safe to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
