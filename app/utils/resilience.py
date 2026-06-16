"""External-API resilience helpers (audit Addition 3).

Outbound calls to third parties (PMS, SMS, Stripe, Clerk) fail in two ways we
must handle differently:

  * **transient** (network blip, 5xx, timeout) — safe to retry IF the operation
    is idempotent. Reads (GET) are; writes that create resources (POST a new
    appointment) are NOT — a retry could double-book. So retry is opt-in, never
    automatic for writes.
  * **deadline** — a live phone call can't wait 15s for a slow PMS. ``with_deadline``
    caps how long the voice path will block before falling back to a callback.

Deliberately dependency-free (no tenacity/backoff): one small, auditable helper
beats a library we'd have to reason about for HIPAA. No jitter — our concurrency
is low (a single backend process) so thundering-herd isn't a concern, and a
deterministic backoff is easier to test and reason about.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger("dentiva.resilience")


def make_timeout(connect: float, read: float) -> httpx.Timeout:
    """Split connect/read timeout. A slow DNS/handshake fails fast (connect),
    while a working-but-slow endpoint gets the longer read budget."""
    return httpx.Timeout(connect=connect, read=read, write=connect, pool=connect)


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    retry_on: type[BaseException] | tuple[type[BaseException], ...],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    label: str = "call",
) -> T:
    """Run ``fn`` with bounded exponential backoff on ``retry_on`` exceptions.

    ONLY use for idempotent operations — a retried write can duplicate. ``attempts``
    is the TOTAL tries (1 = no retry). Backoff is ``base_delay * 2**(n-1)``. The
    last failure is re-raised unchanged so callers see the real error type.

    ``sleep`` is injectable so tests don't actually wait.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.info("%s failed (attempt %s/%s), retrying in %.2fs",
                        label, attempt, attempts, delay)
            await sleep(delay)
    assert last_exc is not None  # loop ran ≥1 time
    raise last_exc


async def with_deadline[T](coro: Awaitable[T], seconds: float, *, label: str = "call") -> T:
    """Cap an awaitable at ``seconds``; raise ``asyncio.TimeoutError`` if exceeded.

    Used on the voice path so a slow third party can't hold the caller on the
    line — the handler catches the timeout and falls back to a callback.
    """
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except TimeoutError:
        logger.warning("%s exceeded %.1fs deadline", label, seconds)
        raise
