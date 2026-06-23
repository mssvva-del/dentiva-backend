"""JWKS cache behaviour (fix/clerk-prod-jwt-401).

The Clerk JWKS cache must be keyed by URL and support a forced refresh, so that
a dev→prod instance switch (CLERK_JWKS_URL change) or a Clerk key rotation does
not serve stale keys and 401 every request until the TTL expires.
"""

from __future__ import annotations

import pytest

import app.middleware.auth as auth

DEV_JWKS = {"keys": [{"kid": "dev-key", "kty": "RSA"}]}
PROD_JWKS = {"keys": [{"kid": "prod-key", "kty": "RSA"}]}


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def fake_jwks(monkeypatch):
    """Patch httpx so each fetch returns a scripted payload and is counted."""
    calls: list[str] = []
    payloads: list[dict] = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            return _FakeResp(payloads.pop(0) if payloads else {"keys": []})

    monkeypatch.setattr(auth.httpx, "AsyncClient", _FakeClient)
    # Reset module-level cache so tests are independent.
    auth._jwks_keys = None
    auth._jwks_fetched_at = 0.0
    auth._jwks_url = None
    yield calls, payloads
    auth._jwks_keys = None
    auth._jwks_fetched_at = 0.0
    auth._jwks_url = None


async def test_same_url_is_cached(fake_jwks):
    calls, payloads = fake_jwks
    payloads.append(DEV_JWKS)
    url = "https://clerk.dev.example/.well-known/jwks.json"

    first = await auth._get_jwks(url)
    second = await auth._get_jwks(url)
    assert first == DEV_JWKS and second == DEV_JWKS
    assert len(calls) == 1  # second call served from cache


async def test_url_change_forces_refetch(fake_jwks):
    calls, payloads = fake_jwks
    payloads.extend([DEV_JWKS, PROD_JWKS])
    dev = "https://clerk.dev.example/.well-known/jwks.json"
    prod = "https://clerk.dentovox.com/.well-known/jwks.json"

    await auth._get_jwks(dev)
    result = await auth._get_jwks(prod)  # different URL → must refetch
    assert result == PROD_JWKS
    assert calls == [dev, prod]


async def test_force_refetch_bypasses_cache(fake_jwks):
    calls, payloads = fake_jwks
    payloads.extend([DEV_JWKS, PROD_JWKS])
    url = "https://clerk.dentovox.com/.well-known/jwks.json"

    await auth._get_jwks(url)
    refreshed = await auth._get_jwks(url, force=True)  # e.g. after a kid miss
    assert refreshed == PROD_JWKS
    assert len(calls) == 2


def test_find_signing_key():
    assert auth._find_signing_key(PROD_JWKS, "prod-key")["kid"] == "prod-key"
    assert auth._find_signing_key(PROD_JWKS, "missing") is None
