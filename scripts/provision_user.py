"""Provision a dashboard User by linking a Clerk account to a practice.

The dashboard authenticates with a Clerk JWT; the backend resolves the caller
via ``users.clerk_user_id == sub``. If no matching User row exists the API
returns 401 "User not provisioned" and the dashboard shows "Couldn't load
data". This script looks the user up in Clerk by email (using
CLERK_SECRET_KEY) and upserts the User row against the target practice.

Usage:
    python3 scripts/provision_user.py --email you@example.com [--role owner]
    python3 scripts/provision_user.py --clerk-user-id user_xxx --email ...

Idempotent: re-running updates email/role/practice for the same clerk_user_id.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure project root importable and .env loaded before app config import.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import httpx  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models.practice import Practice  # noqa: E402
from app.models.user import User  # noqa: E402


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
    # Fall back to first result if email index didn't match exactly.
    if users:
        return users[0]["id"]
    raise SystemExit(f"No Clerk user found for email {email!r}.")


def resolve_db_url(settings) -> str:
    db_url = settings.database_url_sync
    if not db_url:
        db_url = settings.database_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg2://"
        )
    return db_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a dashboard user.")
    parser.add_argument("--email", required=True, help="User email (Clerk + DB).")
    parser.add_argument("--clerk-user-id", help="Skip Clerk lookup; use this id.")
    parser.add_argument(
        "--role", default="owner", choices=["owner", "admin", "staff"]
    )
    parser.add_argument(
        "--practice-id",
        help="Target practice UUID. Defaults to the only/first practice.",
    )
    args = parser.parse_args()

    clerk_user_id = args.clerk_user_id or lookup_clerk_user_id(args.email)
    print(f"Clerk user id: {clerk_user_id}")

    settings = get_settings()
    engine = create_engine(resolve_db_url(settings), echo=False, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            if args.practice_id:
                practice = session.get(Practice, args.practice_id)
            else:
                practice = session.execute(
                    select(Practice).limit(1)
                ).scalars().first()
            if practice is None:
                raise SystemExit(
                    "No practice found. Run scripts/seed_demo.py first."
                )

            existing = session.execute(
                select(User).where(User.clerk_user_id == clerk_user_id)
            ).scalar_one_or_none()

            if existing:
                existing.email = args.email
                existing.role = args.role
                existing.practice_id = practice.id
                action = "Updated"
            else:
                session.add(
                    User(
                        clerk_user_id=clerk_user_id,
                        practice_id=practice.id,
                        email=args.email,
                        role=args.role,
                    )
                )
                action = "Created"
            session.commit()
            print(
                f"{action} user {args.email} ({args.role}) -> "
                f"practice {practice.name} [{practice.id}]"
            )
            print("Dashboard login should now load data.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
