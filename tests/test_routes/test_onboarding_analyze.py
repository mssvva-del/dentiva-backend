"""Smart onboarding (ONB-2.0): analyze-website → prefill + gaps + preview."""

from __future__ import annotations

import pytest
from sqlalchemy import select

import app.services.onboarding_ai as ai
from app.models.practice import Practice
from tests.conftest import seed_practice

_FAKE_PROFILE = {
    "clinic": {"name": "Bright Coast Dental", "address": "12 Ocean Ave, Miami, FL",
               "phone": "+13055550101", "timezone": "America/New_York",
               "languages": ["en", "es"]},
    "business_hours": {"mon": {"open": "08:00", "close": "17:00"},
                       "tue": {"open": "08:00", "close": "17:00"},
                       "wed": None, "thu": {"open": "08:00", "close": "17:00"},
                       "fri": {"open": "08:00", "close": "14:00"},
                       "sat": None, "sun": None},
    "knowledge_base": {
        "providers": [{"name": "Dr. Ana Ruiz", "type": "general", "accepts_new": True}],
        "appointment_types": [{"name": "cleaning", "minutes": 60, "new_patient": False}],
        "insurances": ["Delta Dental", "Cigna"],
        "self_pay": True,
        "policies": {"cancellation": "24h notice", "late": None,
                     "new_patient": None, "parking": None},
    },
    "gaps": [{"field": "emergency", "question": "What number should urgent calls go to?"},
             {"field": "parking", "question": "Is there patient parking?"}],
    "agent_preview": {"greeting": "Thanks for calling Bright Coast Dental!",
                      "sample_answers": [{"q": "Do you take Cigna?",
                                          "a": "Yes, we accept Cigna."}]},
}


def _h(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


async def test_analyze_website_applies_profile(client, db_session, monkeypatch):
    practice, _ = await seed_practice(db_session, name="New practice",
                                      clerk_org_id="org_ai1", clerk_user_id="user_ai1")

    async def fake_analyze(url):  # noqa: ANN001
        assert "brightcoast" in url
        return ai._sane(_FAKE_PROFILE)
    monkeypatch.setattr(ai, "analyze_clinic_website", fake_analyze)

    r = await client.post("/api/onboarding/analyze-website",
                          headers=_h("org_ai1", "user_ai1"),
                          json={"url": "https://brightcoast.example.com"})
    assert r.status_code == 200
    body = r.json()
    # Extraction returned for the UI (gaps + preview included).
    assert body["profile"]["clinic"]["name"] == "Bright Coast Dental"
    assert len(body["profile"]["gaps"]) == 2
    assert body["profile"]["agent_preview"]["greeting"].startswith("Thanks for calling")
    # State reflects the APPLIED profile (wizard reopens prefilled).
    assert body["state"]["name"] == "Bright Coast Dental"

    p = (await db_session.execute(
        select(Practice).where(Practice.id == practice.id)
    )).scalar_one()
    await db_session.refresh(p)
    assert p.name == "Bright Coast Dental"
    assert p.timezone == "America/New_York"
    assert p.business_hours["mon"] == {"open": "08:00", "close": "17:00"}
    assert p.languages_enabled == ["en", "es"]
    kb = p.knowledge_base or {}
    assert kb["insurances"] == ["Delta Dental", "Cigna"]
    assert kb["providers"][0]["name"] == "Dr. Ana Ruiz"


async def test_analyze_website_bad_url_422(client, db_session, monkeypatch):
    await seed_practice(db_session, name="X", clerk_org_id="org_ai2",
                        clerk_user_id="user_ai2")

    async def boom(url):  # noqa: ANN001
        raise ValueError("Please enter a valid website address.")
    monkeypatch.setattr(ai, "analyze_clinic_website", boom)
    r = await client.post("/api/onboarding/analyze-website",
                          headers=_h("org_ai2", "user_ai2"),
                          json={"url": "notaurl"})
    assert r.status_code == 422


async def test_analyze_website_llm_down_502_graceful(client, db_session, monkeypatch):
    await seed_practice(db_session, name="Y", clerk_org_id="org_ai3",
                        clerk_user_id="user_ai3")

    async def boom(url):  # noqa: ANN001
        raise RuntimeError("no_llm_configured")
    monkeypatch.setattr(ai, "analyze_clinic_website", boom)
    r = await client.post("/api/onboarding/analyze-website",
                          headers=_h("org_ai3", "user_ai3"),
                          json={"url": "https://example.com"})
    assert r.status_code == 502
    assert "manually" in r.json()["error"]["message"]


def test_ssrf_guard_blocks_private_hosts():
    assert ai._is_public_host("localhost") is False
    assert ai._is_public_host("127.0.0.1") is False
    assert ai._is_public_host("192.168.1.10") is False
    assert ai._is_public_host("10.0.0.5") is False


@pytest.mark.parametrize("bad", [
    "ftp://example.com",
    "javascript:alert(1)",     # a scheme with no "//" — read as a bare hostname
    "data:text/html,<b>hi</b>",
    "https://example.com:notaport",  # crashed httpx instead of being refused
    "",
])
async def test_fetch_rejects_bad_schemes(bad):
    """Every rejection must be a ValueError the route turns into a message. An
    exception from deeper down is a 500 on user input, in the guard that exists
    to handle user input."""
    with pytest.raises(ValueError):
        await ai.fetch_website_text(bad)


async def test_a_bare_hostname_still_means_https():
    """The clinic types "smiledental.com" — tightening the scheme check must not
    take that away."""
    from unittest.mock import patch

    with patch.object(ai.httpx, "AsyncClient") as client:
        client.side_effect = AssertionError("stop after the guard")
        with pytest.raises(AssertionError):
            await ai.fetch_website_text("smiledental.com")


def test_strip_html_keeps_footer_and_nav():
    # regression: footer/nav hold the NAP (name/address/phone). Stripping them
    # was why address/phone went missing while body hours came through.
    html = (
        "<body><main>Welcome</main>"
        "<footer>Smile Dental · 123 Main St, Orange, CA 92866 · (714) 555-0100</footer>"
        "</body>"
    )
    text = ai._strip_html(html)
    assert "123 Main St" in text
    assert "(714) 555-0100" in text
    # noise is still removed
    assert "alert" not in ai._strip_html("<script>alert(1)</script>hi")


def test_harvest_contact_pulls_tel_and_address():
    html = (
        '<a href="tel:+17145550100">Call us</a>'
        '<a href="tel:+17145550100">again</a>'  # dupe collapses
        "<address>123 Main St, Orange, CA 92866</address>"
    )
    out = ai._harvest_contact(html)
    assert out.startswith("CONTACT:")
    assert "phone: +17145550100" in out
    assert out.count("phone:") == 1  # deduped
    assert "address: 123 Main St, Orange, CA 92866" in out


def test_harvest_contact_empty_when_no_markup():
    assert ai._harvest_contact("<p>just prose, no contact markup</p>") == ""


def test_sane_bounds_garbage():
    junk = {
        "clinic": {"name": "X" * 999, "languages": ["en", "fr", "es"]},
        "business_hours": {"mon": {"open": "9am", "close": "17:00"}},  # bad format
        "knowledge_base": {"providers": [{"name": "Dr. A", "type": "wizard"}],
                           "insurances": [f"Plan{i}" for i in range(50)]},
        "gaps": [{"field": "f", "question": "q"} for _ in range(10)],
        "agent_preview": {},
    }
    out = ai._sane(junk)
    assert len(out["clinic"]["name"]) <= 200
    assert out["clinic"]["languages"] == ["en", "es"]
    assert out["business_hours"]["mon"] is None          # bad HH:MM dropped
    assert out["knowledge_base"]["providers"][0]["type"] == "general"
    assert len(out["knowledge_base"]["insurances"]) <= 15
    assert len(out["gaps"]) <= 5
