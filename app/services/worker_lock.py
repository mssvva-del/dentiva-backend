"""Postgres advisory locks — single-leader election for background workers.

On AWS we run 2+ app instances behind a load balancer. Every instance boots the
same background loops (call-sync, reminders, maintenance, reactivation). With no
coordination they would all fire the same tick and double-send SMS / voice calls
— a TCPA problem (real money, federal footprint) and a patient-trust problem.

A Postgres **session-level advisory lock** gives cheap, dependency-free leader
election: every tick, each instance TRIES the named lock; only the winner runs
the tick, the losers skip it. No Redis, no Celery. It is self-healing — the lock
lives on a dedicated connection held only for the tick, so if the leader crashes
mid-tick the connection drops, Postgres releases the lock, and another instance
wins the next tick. There is no permanent leader to get stuck.

WHY per-tick (not a lock held for the whole process lifetime): a process-lifetime
session lock tied to one pooled connection is fragile — a pool recycle or a
dropped connection silently releases it, and nothing re-acquires until restart.
Re-electing every tick is simpler and strictly safer.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator

from sqlalchemy import text

import app.db as app_db

logger = logging.getLogger(__name__)


def _lock_key(name: str) -> int:
    """Stable signed 64-bit key for a worker name (advisory locks take a bigint).

    Uses hashlib, NOT the builtin ``hash()`` — ``hash()`` is per-process salted
    (PYTHONHASHSEED), so two instances would derive DIFFERENT keys for the same
    name and never contend on the same lock, silently defeating the election.
    blake2b is deterministic across processes and restarts.
    """
    digest = hashlib.blake2b(name.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextlib.asynccontextmanager
async def advisory_tick_lock(name: str) -> AsyncIterator[bool]:
    """Try to hold the named advisory lock for the duration of one worker tick.

    Yields ``True`` to the single winning instance and ``False`` to every other
    instance (and on any DB error — fail-closed, so a transient DB blip makes an
    instance skip the tick rather than run it unguarded).

    The lock sits on a dedicated connection acquired from the engine pool. We
    commit immediately after taking it so the connection is NOT left idle-in-
    transaction for the whole (possibly slow) tick; the advisory lock is
    session-scoped, so it survives that commit and is only released by the
    explicit unlock or by the connection closing.
    """
    key = _lock_key(name)
    try:
        conn = await app_db.engine.connect()
    except Exception:  # noqa: BLE001 — DB unreachable → skip this tick, don't crash the loop
        logger.warning("advisory lock %s: could not open connection; skipping tick", name)
        yield False
        return

    got = False
    try:
        got = bool(
            (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar()
        )
        # Release the transaction (not the session lock) so we don't idle-in-tx.
        await conn.commit()
        yield got
    except Exception:  # noqa: BLE001 — fail closed on any lock error
        logger.warning("advisory lock %s: acquire failed; skipping tick", name, exc_info=True)
        yield False
    finally:
        if got:
            with contextlib.suppress(Exception):
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()
        with contextlib.suppress(Exception):
            await conn.close()
