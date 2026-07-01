"""Backfill patients.phone_hmac for rows created before the column existed.

Run ONCE after migration x9s0t1u2v3w4. Idempotent: re-running only recomputes the
same hashes. Runs as the migration/superuser DB user (bypasses RLS) so it sees
every practice's patients. New rows don't need this — the model event listeners
keep phone_hmac in sync on insert/update.

    python -m scripts.backfill_phone_hmac
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

import app.db as app_db
from app.models.patient import Patient
from app.utils.crypto import phone_hmac


async def main() -> None:
    updated = 0
    async with app_db.async_session_factory() as session:
        patients = (await session.execute(select(Patient))).scalars().all()
        for p in patients:
            try:
                h = phone_hmac(p.phone)  # p.phone decrypts transparently
            except Exception:  # noqa: BLE001 — a stray undecryptable row must not stop the backfill
                h = None
            if p.phone_hmac != h:
                p.phone_hmac = h
                updated += 1
        await session.commit()
    print(f"backfill complete: {updated} patient rows updated")


if __name__ == "__main__":
    asyncio.run(main())
