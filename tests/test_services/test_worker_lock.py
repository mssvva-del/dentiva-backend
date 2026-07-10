"""Advisory-lock leader election — the guard that stops 2+ AWS instances from
running the same background tick (double SMS/voice). We prove: only one holder
at a time per name, different names don't contend, the key is process-stable,
and a DB error fails closed (skip the tick, never run unguarded)."""

from __future__ import annotations

import app.db as app_db
from app.services.worker_lock import _lock_key, advisory_tick_lock


def test_lock_key_is_deterministic_and_fits_bigint():
    # Same name → same key across calls (must also hold across processes, which
    # is why the impl uses hashlib, not the salted builtin hash()).
    assert _lock_key("reminders") == _lock_key("reminders")
    assert _lock_key("reminders") != _lock_key("call_sync")
    # Signed 64-bit range (Postgres bigint).
    for name in ("reminders", "call_sync", "maintenance", "reactivation"):
        assert -(2**63) <= _lock_key(name) < 2**63


async def test_only_one_holder_at_a_time(_prepare_database):
    # First holder wins; a second acquire of the SAME name on a different
    # connection is refused while the first is held.
    async with advisory_tick_lock("dup_name") as first:
        assert first is True
        async with advisory_tick_lock("dup_name") as second:
            assert second is False
    # Released on exit → can be re-acquired.
    async with advisory_tick_lock("dup_name") as again:
        assert again is True


async def test_distinct_names_dont_contend(_prepare_database):
    async with advisory_tick_lock("name_a") as a, advisory_tick_lock("name_b") as b:
        assert a is True and b is True


async def test_fails_closed_on_db_error(_prepare_database, monkeypatch):
    # If the DB is unreachable the tick must be SKIPPED (yield False), not run
    # without the guard.
    class _BoomEngine:
        async def connect(self):  # noqa: ANN001
            raise OSError("db down")

    monkeypatch.setattr(app_db, "engine", _BoomEngine())
    async with advisory_tick_lock("whatever") as leader:
        assert leader is False
