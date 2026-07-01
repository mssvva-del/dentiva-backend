"""Retell custom-LLM relay protocol — WebSocket frames (mocked Groq + practice)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.api.llm_relay as relay
from app.api.llm_relay import router
from app.services.llm.groq_client import GroqError


def _app() -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # call_details prompt resolution shouldn't hit the DB in a protocol test.
    async def _no_practice(_agent_id):
        return None
    monkeypatch.setattr(relay, "_resolve_practice", _no_practice)


def _fake_stream(*tokens):
    async def _gen(_messages, **_kw):
        for t in tokens:
            yield t
    return _gen


def test_config_frame_and_ping_pong(monkeypatch):
    monkeypatch.setattr(relay, "stream_chat", _fake_stream("hi"))
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c1") as ws:
        cfg = ws.receive_json()
        assert cfg["response_type"] == "config" and cfg["config"]["call_details"] is True
        ws.send_json({"interaction_type": "ping_pong", "timestamp": 42})
        assert ws.receive_json() == {"response_type": "ping_pong", "timestamp": 42}


def test_response_required_streams_then_completes(monkeypatch):
    monkeypatch.setattr(relay, "stream_chat", _fake_stream("Hello", " there"))
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c2") as ws:
        ws.receive_json()  # config
        ws.send_json({
            "interaction_type": "response_required",
            "response_id": 7,
            "transcript": [{"role": "user", "content": "hi"}],
        })
        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            if f.get("content_complete"):
                break
        assert all(f["response_id"] == 7 for f in frames)
        assert "".join(f["content"] for f in frames) == "Hello there"
        assert frames[-1]["content_complete"] is True
        assert frames[-1]["content"] == ""


def test_groq_failure_speaks_fallback(monkeypatch):
    async def _boom(_messages, **_kw):
        raise GroqError("down")
        yield  # pragma: no cover — makes this an async generator
    monkeypatch.setattr(relay, "stream_chat", _boom)
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c3") as ws:
        ws.receive_json()  # config
        ws.send_json({
            "interaction_type": "response_required",
            "response_id": 1,
            "transcript": [{"role": "user", "content": "hi"}],
        })
        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            if f.get("content_complete"):
                break
        joined = "".join(f["content"] for f in frames)
        assert "trouble" in joined              # fallback line spoken
        assert frames[-1]["content_complete"] is True


def test_update_only_produces_no_frame(monkeypatch):
    monkeypatch.setattr(relay, "stream_chat", _fake_stream("x"))
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c4") as ws:
        ws.receive_json()  # config
        ws.send_json({"interaction_type": "update_only", "transcript": []})
        # Follow with a ping to prove the socket is alive and update_only was a no-op.
        ws.send_json({"interaction_type": "ping_pong", "timestamp": 9})
        assert ws.receive_json() == {"response_type": "ping_pong", "timestamp": 9}


def _drain(ws) -> list:
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f.get("content_complete"):
            return frames


def test_stall_speaks_fallback(monkeypatch):
    # First-token budget breached → fallback, no dead air.
    monkeypatch.setattr(relay, "_FIRST_TOKEN_TIMEOUT", 0.05)

    async def _stall(_messages, **_kw):
        await asyncio.sleep(1.0)
        yield "late"  # pragma: no cover — never reached before timeout
    monkeypatch.setattr(relay, "stream_chat", _stall)
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c5") as ws:
        ws.receive_json()  # config
        ws.send_json({"interaction_type": "response_required", "response_id": 1,
                      "transcript": [{"role": "user", "content": "hi"}]})
        frames = _drain(ws)
        assert "trouble" in "".join(f["content"] for f in frames)
        assert frames[-1]["content_complete"] is True


def test_partial_stream_then_error_no_fallback(monkeypatch):
    # Tokens already spoken → do NOT append the fallback, just close the turn.
    async def _partial(_messages, **_kw):
        yield "Half a "
        raise GroqError("mid-stream drop")
    monkeypatch.setattr(relay, "stream_chat", _partial)
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c6") as ws:
        ws.receive_json()  # config
        ws.send_json({"interaction_type": "response_required", "response_id": 2,
                      "transcript": [{"role": "user", "content": "hi"}]})
        frames = _drain(ws)
        joined = "".join(f["content"] for f in frames)
        assert joined == "Half a "         # partial content, NO fallback appended
        assert "trouble" not in joined
        assert frames[-1]["content_complete"] is True


def test_reminder_required_also_responds(monkeypatch):
    monkeypatch.setattr(relay, "stream_chat", _fake_stream("Still there?"))
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c7") as ws:
        ws.receive_json()  # config
        ws.send_json({"interaction_type": "reminder_required", "response_id": 3,
                      "transcript": [{"role": "agent", "content": "Hi"}]})
        frames = _drain(ws)
        assert "".join(f["content"] for f in frames) == "Still there?"


def test_malformed_frame_does_not_drop_call(monkeypatch):
    monkeypatch.setattr(relay, "stream_chat", _fake_stream("ok"))
    with TestClient(_app()).websocket_connect("/ws/retell-llm/c8") as ws:
        ws.receive_json()  # config
        ws.send_text("this is not json")            # malformed → dropped, call lives
        ws.send_json({"interaction_type": "ping_pong", "timestamp": 5})
        assert ws.receive_json() == {"response_type": "ping_pong", "timestamp": 5}


def test_call_details_swaps_in_practice_prompt(monkeypatch):
    captured = {}

    async def _capture(messages, **_kw):
        captured["system"] = messages[0]["content"]
        yield "hi"
    monkeypatch.setattr(relay, "stream_chat", _capture)

    async def _practice(_agent_id):
        return SimpleNamespace(
            name="Bright Smiles NJ", business_hours={}, address=None,
            languages_enabled=["en", "es"], knowledge_base={},
        )
    monkeypatch.setattr(relay, "_resolve_practice", _practice)

    with TestClient(_app()).websocket_connect("/ws/retell-llm/c9") as ws:
        ws.receive_json()  # config
        ws.send_json({"interaction_type": "call_details",
                      "call": {"agent_id": "agent_x"}})
        ws.send_json({"interaction_type": "response_required", "response_id": 1,
                      "transcript": [{"role": "user", "content": "hi"}]})
        _drain(ws)
    assert "Bright Smiles NJ" in captured["system"]  # clinic-aware prompt reached Groq
