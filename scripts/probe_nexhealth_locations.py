#!/usr/bin/env python3
"""How does our NexHealth account describe the practices connected to it?

Linking a clinic to its calendar is the last thing anyone still types by hand.
The account key is ONE key for all of our practices — so what a clinic actually
needs is not credentials but the id of its own location inside our account, and
an admin picking that from a list is the whole job.

Which means one question has to be answered before any of it is built: what does
the account return when asked what is connected to it? Institutions, locations,
both, under which field names, and is a subdomain required to ask.

READ-ONLY. This script creates nothing, changes nothing, and deletes nothing.
Unlike the patient probe it is safe against the PRODUCTION account, because
looking at a list is not an act.

WHAT IT PRINTS: status codes, field NAMES, and counts. Practice NAMES are shown
(they are our own customers, not patients) but no patient data is fetched.

Usage — put NEXHEALTH_API_KEY in dentiva-backend/.env, then:

    .venv/bin/python scripts/probe_nexhealth_locations.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://nexhealth.info"
HEADERS = {
    "Accept": "application/vnd.Nexhealth+json;version=2",
    # Cloudflare blocks the default Python agent with a 1010/403.
    "User-Agent": "Dentovox/1.0 (+https://dentovox.com)",
    "Content-Type": "application/json",
}


def key_from_env_file() -> str:
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


def authenticate(api_key: str) -> str | None:
    request = urllib.request.Request(
        f"{BASE}/authenticates", data=b"",
        headers={**HEADERS, "Authorization": api_key}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return (json.loads(response.read() or b"{}").get("data") or {}).get("token")
    except urllib.error.HTTPError as exc:
        print(f"  authentication FAILED with {exc.code} — the key was not accepted")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"  authentication failed: {type(exc).__name__}: {exc}")
        return None


def get(path: str, token: str, params: dict | None = None) -> tuple[int, object]:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={**HEADERS, "Authorization": f"Bearer {token}"}, method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}").get("data")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, None
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def describe(label: str, status: int, data: object) -> list[dict]:
    """Print the shape without printing the contents."""
    rows: list = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # Their envelope sometimes nests the list one level down.
        for key in ("locations", "institutions", "items"):
            if isinstance(data.get(key), list):
                rows = data[key]
                print(f"  {status}  {label} → data.{key}, {len(rows)} row(s)")
                break
        else:
            print(f"  {status}  {label} → data is an object: {sorted(data.keys())}")
            return []
    if isinstance(data, list):
        print(f"  {status}  {label} → data is a list, {len(rows)} row(s)")
    if not rows:
        if not isinstance(data, dict):
            print(f"  {status}  {label} → {str(data)[:200]}")
        return []
    first = rows[0] if isinstance(rows[0], dict) else {}
    print(f"       fields: {sorted(first.keys())}")
    return [r for r in rows if isinstance(r, dict)]


def main() -> int:
    api_key = (os.getenv("NEXHEALTH_API_KEY") or key_from_env_file()).strip()
    if not api_key:
        print("NEXHEALTH_API_KEY is not set. Put it in dentiva-backend/.env and run again.")
        return 2

    print("[1] authenticating")
    token = authenticate(api_key)
    if not token:
        return 1
    print("  authenticated")

    print("\n[2] what is connected to this account")
    institutions: list[dict] = []
    for path, params in (
        ("/institutions", {}),
        ("/institutions", {"per_page": 50}),
    ):
        status, data = get(path, token, params)
        institutions = describe(f"GET {path} {params or ''}".strip(), status, data) or institutions
        if institutions:
            break

    for row in institutions[:10]:
        # Our own customers' practice names — not patient data.
        print(f"       · id={row.get('id')} name={row.get('name')!r} "
              f"subdomain={row.get('subdomain')!r}")

    print("\n[3] the locations a clinic would be matched to")
    # subdomain scopes most endpoints. Try with and without, and with each
    # institution's own subdomain, because which one is required is the thing
    # that decides how the admin screen has to be built.
    attempts: list[tuple[str, dict]] = [("/locations", {"per_page": 50})]
    for row in institutions[:3]:
        sub = row.get("subdomain")
        if sub:
            attempts.append(("/locations", {"subdomain": sub, "per_page": 50}))
    if institutions and institutions[0].get("id"):
        attempts.append(("/locations", {"institution_id": institutions[0]["id"],
                                        "per_page": 50}))

    locations: list[dict] = []
    for path, params in attempts:
        status, data = get(path, token, params)
        rows = describe(f"GET {path} {params}", status, data)
        if rows and not locations:
            locations = rows

    for row in locations[:10]:
        print(f"       · id={row.get('id')} name={row.get('name')!r} "
              f"institution_id={row.get('institution_id')}")

    print("\nRESULT")
    if locations:
        print(f"  {len(locations)} location(s) readable from the account key alone.")
        print("  An admin can pick a clinic's location from a list — nobody types an id,")
        print("  and no clinic ever sees another clinic's practice.")
    else:
        print("  No location list came back. Paste this whole output; the admin screen")
        print("  will have to take an id typed in until we know how to ask for one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
