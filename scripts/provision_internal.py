"""Provision a Dentiva INTERNAL staff member (admin world).

Internal staff are NOT created via self-serve signup. A super_admin runs this
after the person has signed up to Clerk (so a Clerk user exists). It:
  1. looks the Clerk user up by email (CLERK_SECRET_KEY),
  2. upserts a `users` row with is_internal=True, practice_id=NULL,
  3. upserts a `dentiva_staff` row with the given Dentiva role.

Usage:
    python3 scripts/provision_internal.py --email you@example.com --role super_admin

Roles: super_admin | support | sales | finance | engineer  (see app/auth/permissions.py)
Idempotent: re-running updates the role for the same Clerk user.

NOTE: against the Railway DB, export DATABASE_URL to the public URL first (see
scripts/seed_railway.sh for how it resolves _docs/_railway_db.txt).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import httpx  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models.dentiva_staff import DentivaStaff  # noqa: E402
from app.models.user import User  # noqa: E402

VALID_ROLES = {"super_admin", "support", "sales", "finance", "engineer"}


def lookup_clerk_user_id(email: str) -> str:
    secret = os.environ.get("CLERK_SECRET_KEY")
    if not secret:
        raise SystemExit("CLERK_SECRET_KEY not set; cannot look up Clerk user.")
    resp = httpx.get(
        "https://api.clerk.com/v1/users",
        params={"email_address": [email], "limit": 10},
        headers={"Authorization": f"Bearer {secret}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    users = data if isinstance(data, list) else data.get("data", [])
    for u in users:
        for ea in u.get("email_addresses", []):
            if ea.get("email_address", "").lower() == email.lower():
                return u["id"]
    if users:
        return users[0]["id"]
    raise SystemExit(f"No Clerk user found for email {email!r}. Have them sign up first.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--role", required=True, choices=sorted(VALID_ROLES))
    ap.add_argument("--clerk-user-id", default=None, help="skip Clerk lookup")
    args = ap.parse_args()

    clerk_id = args.clerk_user_id or lookup_clerk_user_id(args.email)
    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)

    with Session(engine) as s:
        user = s.execute(
            select(User).where(User.clerk_user_id == clerk_id)
        ).scalar_one_or_none()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                clerk_user_id=clerk_id,
                practice_id=None,        # internal staff belong to no clinic
                email=args.email,
                role="staff",            # clinic role unused for internal
                is_internal=True,
                status="active",
            )
            s.add(user)
            s.flush()
        else:
            user.is_internal = True
            user.email = args.email

        staff = s.execute(
            select(DentivaStaff).where(DentivaStaff.user_id == user.id)
        ).scalar_one_or_none()
        if staff is None:
            s.add(DentivaStaff(id=uuid.uuid4(), user_id=user.id, role=args.role))
        else:
            staff.role = args.role

        s.commit()
        print(f"Provisioned internal staff {args.email} as {args.role} (clerk={clerk_id}).")


if __name__ == "__main__":
    main()
