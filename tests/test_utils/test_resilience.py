"""External-API resilience helpers (audit Addition 3)."""

from __future__ import annotations

import asyncio

import pytest

from app.utils.resilience import make_timeout, retry_async, with_deadline


async def _nosleep(_seconds: float) -> None:
    return None


def test_make_timeout_splits_connect_and_read():
    t = make_timeout(connect=5.0, read=15.0)
    assert t.connect == 5.0
    assert t.read == 15.0


async def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return "ok"

    out = await retry_async(
        flaky, attempts=3, base_delay=0.01, retry_on=ConnectionError, sleep=_nosleep
    )
    assert out == "ok"
    assert calls["n"] == 3


async def test_retry_reraises_after_exhausting_attempts():
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await retry_async(
            always_fails, attempts=2, base_delay=0.01,
            retry_on=ConnectionError, sleep=_nosleep,
        )
    assert calls["n"] == 2  # total tries == attempts, no more


async def test_retry_does_not_catch_other_exceptions():
    async def boom():
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        await retry_async(
            boom, attempts=3, base_delay=0.01,
            retry_on=ConnectionError, sleep=_nosleep,
        )


async def test_retry_backoff_is_exponential():
    delays: list[float] = []

    async def record_sleep(s: float) -> None:
        delays.append(s)

    async def always_fails():
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        await retry_async(
            always_fails, attempts=4, base_delay=0.1,
            retry_on=ConnectionError, sleep=record_sleep,
        )
    # 3 sleeps between 4 attempts: 0.1, 0.2, 0.4.
    assert delays == [0.1, pytest.approx(0.2), pytest.approx(0.4)]


async def test_with_deadline_passes_through_fast_result():
    async def quick():
        return 42

    assert await with_deadline(quick(), seconds=1.0) == 42


async def test_with_deadline_raises_on_slow():
    async def slow():
        await asyncio.sleep(0.5)
        return "late"

    with pytest.raises(asyncio.TimeoutError):
        await with_deadline(slow(), seconds=0.01)
