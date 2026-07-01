"""Groq streaming client — SSE parsing + error handling (mocked transport)."""

from __future__ import annotations

import httpx
import pytest

from app.services.llm import groq_client
from app.services.llm.groq_client import GroqError, stream_chat


class _Cfg:
    groq_api_key = "test-key"
    groq_base_url = "https://groq.test/openai/v1"
    llm_model = "llama-3.3-70b-versatile"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(groq_client, "get_settings", lambda: _Cfg())


def _sse(*chunks: str) -> bytes:
    lines = [
        'data: {"choices":[{"delta":{"content":"' + c + '"}}]}\n\n' for c in chunks
    ]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


async def test_stream_yields_deltas_in_order():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse("Hola", ", ", "María"))

    out = [d async for d in stream_chat(
        [{"role": "user", "content": "hi"}], transport=httpx.MockTransport(handler)
    )]
    assert "".join(out) == "Hola, María"


async def test_stream_stops_on_done_and_skips_keepalive():
    def handler(_req: httpx.Request) -> httpx.Response:
        body = (
            b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            b"data: \n\n"                                        # keepalive → skipped
            b': comment line\n\n'                               # non-data → skipped
            b'data: {"choices":[{"delta":{}}]}\n\n'             # no content → skipped
            b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
            b"data: [DONE]\n\n"
            b'data: {"choices":[{"delta":{"content":"AFTER"}}]}\n\n'  # after DONE → ignored
        )
        return httpx.Response(200, content=body)

    out = [d async for d in stream_chat(
        [{"role": "user", "content": "hi"}], transport=httpx.MockTransport(handler)
    )]
    assert "".join(out) == "AB"


async def test_4xx_raises_groqerror():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")

    with pytest.raises(GroqError):
        async for _ in stream_chat(
            [{"role": "user", "content": "hi"}], transport=httpx.MockTransport(handler)
        ):
            pass


async def test_missing_key_raises(monkeypatch):
    class _NoKey(_Cfg):
        groq_api_key = ""
    monkeypatch.setattr(groq_client, "get_settings", lambda: _NoKey())
    with pytest.raises(GroqError):
        async for _ in stream_chat([{"role": "user", "content": "hi"}]):
            pass
