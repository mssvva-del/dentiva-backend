"""Creating a clinic from the admin panel failed with "Could not create the
organization in Clerk" and nothing else.

The reason was already in our hands — Clerk answers a 403 with a plain sentence,
"The organizations feature is not enabled for this instance" — and we wrote it to
a log line the operator cannot read and then threw it away. That is the
difference between a toggle in a dashboard and an afternoon of guessing.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import clerk_api


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(
        clerk_api, "get_settings",
        lambda: type("S", (), {"clerk_secret_key": "sk_test_x"})(),
    )


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def clerk(monkeypatch):
    """Point the module's httpx client at a handler of the test's choosing."""

    def _install(handler):
        real = httpx.AsyncClient

        def factory(*a, **kw):
            kw["transport"] = _transport(handler)
            return real(*a, **kw)

        monkeypatch.setattr(clerk_api.httpx, "AsyncClient", factory)

    return _install


@pytest.mark.asyncio
async def test_the_operator_is_told_what_clerk_said(clerk):
    clerk(lambda r: httpx.Response(403, json={"errors": [{
        "message": "access denied",
        "long_message": "The organizations feature is not enabled for this "
                        "instance. You can enable it at https://dashboard.clerk.com.",
        "code": "organization_not_enabled_in_instance",
    }]}))
    org_id, why = await clerk_api.create_organization_detailed(name="Test Clinic")
    assert org_id is None
    assert "organizations feature is not enabled" in why
    assert "403" in why


@pytest.mark.asyncio
async def test_success_returns_the_id_and_no_complaint(clerk):
    clerk(lambda r: httpx.Response(200, json={"id": "org_123"}))
    assert await clerk_api.create_organization_detailed(name="X") == ("org_123", "")


@pytest.mark.asyncio
async def test_the_old_signature_still_answers_none(clerk):
    # bulk_onboarding imports the simple form and must keep treating None as
    # "skip this row" — one bad clinic cannot abort an import of two hundred.
    clerk(lambda r: httpx.Response(422, json={"errors": [{"message": "nope"}]}))
    assert await clerk_api.create_organization(name="X") is None


@pytest.mark.asyncio
async def test_a_non_json_error_page_is_not_a_crash(clerk):
    clerk(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    org_id, why = await clerk_api.create_organization_detailed(name="X")
    assert org_id is None
    assert "502" in why


@pytest.mark.asyncio
async def test_an_unreachable_clerk_says_so(clerk):
    def boom(_request):
        raise httpx.ConnectError("no route to host")

    clerk(boom)
    org_id, why = await clerk_api.create_organization_detailed(name="X")
    assert org_id is None
    assert "Could not reach Clerk" in why


@pytest.mark.asyncio
async def test_a_missing_key_names_the_variable(monkeypatch):
    monkeypatch.setattr(
        clerk_api, "get_settings",
        lambda: type("S", (), {"clerk_secret_key": ""})(),
    )
    org_id, why = await clerk_api.create_organization_detailed(name="X")
    assert org_id is None
    assert "CLERK_SECRET_KEY" in why
