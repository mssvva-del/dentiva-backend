"""Groq chat streaming (OpenAI-compatible SSE).

Groq exposes an OpenAI-compatible API, so we stream chat completions over plain
httpx SSE — no extra SDK. Used by the Retell custom-LLM relay to generate the
receptionist's turns on Groq Llama-3.3-70b (sub-second, our own loop) instead of
Retell's managed model.

Yields text deltas as they arrive so the relay can forward them to Retell token by
token (low time-to-first-word). Kept transport-injectable for tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import get_settings

logger = logging.getLogger("dentiva.llm.groq")


class GroqError(Exception):
    """Non-recoverable Groq error (4xx / malformed) — the relay falls back."""


# Pooled client: a live call makes one Groq request per turn, so reusing a keep-alive
# connection avoids paying TLS/connection setup on every turn (time-to-first-word).
# Tests pass their own transport, which bypasses the pool with an ephemeral client.
_pool: httpx.AsyncClient | None = None


def _pooled_client(timeout: float) -> httpx.AsyncClient:
    global _pool
    if _pool is None or _pool.is_closed:
        _pool = httpx.AsyncClient(timeout=timeout)
    return _pool


async def aclose_pool() -> None:
    """Close the pooled client (call on app shutdown)."""
    global _pool
    if _pool is not None and not _pool.is_closed:
        await _pool.aclose()
    _pool = None


async def stream_chat(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 220,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> AsyncIterator[str]:
    """Stream assistant text deltas for a chat completion.

    ``messages`` is the OpenAI-shaped list (system + turns). Yields content
    fragments in order; stops on the ``[DONE]`` sentinel. Raises GroqError on a
    non-2xx so the caller can speak a safe fallback line rather than go silent.
    Wall-clock timeouts (first-token / stall) are enforced by the CALLER around the
    iterator so it can emit a spoken fallback on a stall.
    """
    s = get_settings()
    if not s.groq_api_key:
        raise GroqError("GROQ_API_KEY not configured")
    payload = {
        "model": model or s.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {s.groq_api_key}",
        "Content-Type": "application/json",
    }
    url = s.groq_base_url.rstrip("/") + "/chat/completions"
    # Ephemeral client for injected transport (tests); pooled client otherwise.
    client = (
        httpx.AsyncClient(transport=transport, timeout=timeout)
        if transport is not None
        else _pooled_client(timeout)
    )
    owns_client = transport is not None
    try:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                # Drain a little for the log; never echo full body (may be large).
                body = (await resp.aread())[:200]
                raise GroqError(f"Groq {resp.status_code}: {body!r}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue  # keepalive / shape drift — skip, don't crash the turn
                if delta:
                    yield delta
    finally:
        if owns_client:
            await client.aclose()
