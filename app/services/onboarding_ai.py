"""Smart onboarding — learn the clinic from its OWN website (ONB-2.0).

The doctor pastes their site URL; we fetch a few public pages, hand the text to
an LLM, and get back a prefilled clinic profile in OUR KnowledgeBase schema plus
gap questions (the 3-5 things the site didn't answer) and a short "how your
agent will sound" preview. The wizard opens prefilled — the doctor confirms
instead of typing.

Safety: outbound fetch is SSRF-guarded (public http/https hosts only, no private
IPs), capped in size/time. LLM: Anthropic (primary) or Groq (fallback) — keys
come from env; when neither is set the endpoint degrades gracefully and the
wizard just stays manual.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger("dentiva.onboarding_ai")

_MAX_PAGE_BYTES = 400_000
_MAX_TOTAL_CHARS = 24_000
_SUBPAGES = (
    "about", "about-us", "services", "contact", "contact-us", "team",
    "our-team", "insurance", "location", "locations", "hours", "appointments",
)

_EXTRACT_PROMPT = """You are configuring an AI phone receptionist for a US dental practice.
From the website text below, extract ONLY facts that are actually present.
Respond with a SINGLE JSON object, no prose, exactly this shape:

{
  "clinic": {"name": str|null, "address": str|null, "phone": str|null,
             "timezone": one of ["America/New_York","America/Chicago","America/Denver",
                "America/Phoenix","America/Los_Angeles","America/Anchorage",
                "Pacific/Honolulu"]|null,
             "languages": subset of ["en","es"]},
  "business_hours": {"mon"|"tue"|"wed"|"thu"|"fri"|"sat"|"sun":
                     {"open":"HH:MM","close":"HH:MM"} or null, ... all 7 keys},
  "knowledge_base": {
    "providers": [{"name": str,
                   "type": "general"|"hygienist"|"orthodontist"|"surgeon"|"other",
                   "accepts_new": bool}],
    "appointment_types": [{"name": str, "minutes": int|null, "new_patient": bool}],
    "insurances": [str],
    "self_pay": bool|null,
    "current_offer": str|null,
    "policies": {"cancellation": str|null, "late": str|null,
                 "new_patient": str|null, "parking": str|null}
  },
  "gaps": [{"field": str, "question": str}],
  "agent_preview": {"greeting": str, "sample_answers": [{"q": str, "a": str}]}
}

Rules:
- If a line starting with "CONTACT:" appears, it holds the practice's real phone
  and address pulled straight from the page markup — treat it as authoritative
  for the "phone" and "address" fields.
- phone: return US digits, formatted "+1XXXXXXXXXX" when you can.
- current_offer: the practice's OWN promotion/special if the site advertises one
  (e.g. "New patient exam + X-rays $99", "Free whitening with new-patient visit").
  This is the clinic's marketing offer, NOT insurance. null if none is shown.
- NEVER invent facts. Anything not on the site → null/[] and add a gap.
- gaps: the 3-5 MOST important missing pieces for a receptionist (e.g. insurances
  not listed, no emergency/after-hours number, hours missing, cancellation policy
  absent). Each question is ONE short, doctor-friendly question.
- timezone: infer from the address's state if present, else null.
- agent_preview: greeting = one warm sentence naming the clinic; sample_answers =
  3 short Q&A pairs a patient might ask, answered ONLY from extracted facts.

WEBSITE TEXT:
"""


def _is_public_host(host: str) -> bool:
    """SSRF guard: resolve the host and require every address to be public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def _harvest_contact(html: str) -> str:
    """Pull the name/address/phone that live in markup, not visible prose.

    A dental site's phone and address almost always sit in the footer as a
    ``tel:`` link and an ``<address>`` block. We surface them as an explicit
    CONTACT line so the LLM can't miss them even if the surrounding layout is
    noisy — this is exactly what used to be lost when the footer was stripped.
    """
    tels = re.findall(r'href=["\']tel:([^"\']+)["\']', html, re.I)
    addrs = re.findall(r"(?is)<address[^>]*>(.*?)</address>", html)
    bits: list[str] = []
    for t in dict.fromkeys(tels):  # dedupe, keep order
        bits.append(f"phone: {t.strip()}")
    for a in addrs:
        clean = re.sub(r"(?s)<[^>]+>", " ", a)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            bits.append(f"address: {clean}")
    return ("CONTACT: " + " | ".join(bits) + "\n") if bits else ""


def _strip_html(html: str) -> str:
    # Strip only noise. KEEP <footer>/<nav>/<header> — the name, address and
    # phone (NAP) usually live in the footer; removing it was why address/phone
    # went missing while body-widget hours came through.
    html = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&[a-z#0-9]+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()


async def fetch_website_text(url: str) -> str:
    """Fetch the homepage + a few likely subpages as plain text. Raises ValueError
    on an unusable/unsafe URL."""
    # A clinic types "smiledental.com", so a missing scheme means https. But
    # "javascript:alert(1)" HAS a scheme — it simply has no "//" — and testing for
    # "://" mistook it for a bare hostname, prefixed https:// and produced
    # "https://javascript:alert(1)". That passed the scheme check, then blew up
    # inside httpx parsing "alert(1)" as a port: a 500 on user input, in the guard
    # whose job is to reject user input safely.
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = urlparse(f"https://{url}")
    try:
        _ = parsed.port  # a non-numeric port raises here rather than inside httpx
    except ValueError:
        raise ValueError("Please enter a valid website address.") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please enter a valid website address.")
    if not _is_public_host(parsed.hostname):
        raise ValueError("That address can't be reached.")
    base = f"{parsed.scheme}://{parsed.netloc}"

    chunks: list[str] = []
    async with httpx.AsyncClient(
        timeout=12, follow_redirects=True,
        headers={"User-Agent": "DentovoxSetup/1.0 (+https://dentovox.com)"},
    ) as client:
        async def grab(u: str) -> None:
            if sum(len(c) for c in chunks) >= _MAX_TOTAL_CHARS:
                return
            try:
                r = await client.get(u)
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", "html"):
                    raw = r.text[:_MAX_PAGE_BYTES]
                    chunks.append(_harvest_contact(raw) + _strip_html(raw))
            except httpx.HTTPError:
                pass  # a missing subpage is normal

        await grab(base)
        if not chunks:
            raise ValueError("Couldn't read that website — check the address.")
        for path in _SUBPAGES:
            await grab(urljoin(base + "/", path))

    return " \n".join(chunks)[:_MAX_TOTAL_CHARS]


async def _anthropic_json(prompt: str, key: str) -> str:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5", "max_tokens": 3000,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]


async def _groq_json(prompt: str, settings) -> str:  # noqa: ANN001
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{settings.groq_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={"model": settings.llm_model, "max_tokens": 3000,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def _llm_json(prompt: str) -> dict:
    """One-shot JSON extraction. Anthropic Haiku is primary (cheapest, best JSON);
    if it's absent OR errors (e.g. no credit), fall back to Groq — the feature
    must not break just because one provider is down/unfunded."""
    settings = get_settings()
    text: str | None = None
    if settings.anthropic_api_key:
        try:
            text = await _anthropic_json(prompt, settings.anthropic_api_key)
        except Exception as exc:  # noqa: BLE001 — fall through to Groq
            logger.warning("onboarding_ai: Anthropic failed (%s), trying Groq", exc)
    if text is None:
        if not settings.groq_api_key:
            raise RuntimeError("no_llm_configured")
        text = await _groq_json(prompt, settings)

    # Tolerate a fenced/prefixed JSON body.
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("extraction_unparseable")
    return json.loads(match.group(0))


def _sane(profile: dict) -> dict:
    """Bound/normalize the LLM output so garbage can't reach the wizard."""
    out: dict = {"clinic": {}, "business_hours": {}, "knowledge_base": {},
                 "gaps": [], "agent_preview": {}}
    clinic = profile.get("clinic") or {}
    out["clinic"] = {
        "name": (str(clinic.get("name"))[:200] if clinic.get("name") else None),
        "address": (str(clinic.get("address"))[:300] if clinic.get("address") else None),
        "phone": (str(clinic.get("phone"))[:20] if clinic.get("phone") else None),
        "timezone": clinic.get("timezone"),
        "languages": [x for x in (clinic.get("languages") or ["en"])
                      if x in ("en", "es")] or ["en"],
    }
    hours = profile.get("business_hours") or {}
    hhmm = re.compile(r"^\d{2}:\d{2}$")
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        v = hours.get(day)
        if (isinstance(v, dict) and hhmm.match(str(v.get("open", "")))
                and hhmm.match(str(v.get("close", "")))):
            out["business_hours"][day] = {"open": v["open"], "close": v["close"]}
        else:
            out["business_hours"][day] = None
    kb = profile.get("knowledge_base") or {}
    out["knowledge_base"] = {
        "providers": [
            {"name": str(p.get("name"))[:120],
             "type": p.get("type") if p.get("type") in
                     ("general", "hygienist", "orthodontist", "surgeon", "other") else "general",
             "accepts_new": bool(p.get("accepts_new", True))}
            for p in (kb.get("providers") or []) if isinstance(p, dict) and p.get("name")
        ][:12],
        "appointment_types": [
            {"name": str(t.get("name"))[:80],
             "minutes": int(t["minutes"]) if isinstance(t.get("minutes"), int) else None,
             "new_patient": bool(t.get("new_patient", False))}
            for t in (kb.get("appointment_types") or []) if isinstance(t, dict) and t.get("name")
        ][:12],
        "insurances": [str(i)[:60] for i in (kb.get("insurances") or [])][:15],
        "self_pay": kb.get("self_pay") if isinstance(kb.get("self_pay"), bool) else None,
        "current_offer": (str(kb.get("current_offer"))[:300]
                          if kb.get("current_offer") else None),
        "policies": {
            k: (str(v)[:300] if v else None)
            for k, v in (kb.get("policies") or {}).items()
            if k in ("cancellation", "late", "new_patient", "parking")
        },
    }
    out["gaps"] = [
        {"field": str(g.get("field"))[:60], "question": str(g.get("question"))[:200]}
        for g in (profile.get("gaps") or []) if isinstance(g, dict) and g.get("question")
    ][:5]
    prev = profile.get("agent_preview") or {}
    out["agent_preview"] = {
        "greeting": str(prev.get("greeting") or "")[:300],
        "sample_answers": [
            {"q": str(s.get("q"))[:150], "a": str(s.get("a"))[:300]}
            for s in (prev.get("sample_answers") or []) if isinstance(s, dict) and s.get("q")
        ][:3],
    }
    return out


async def analyze_clinic_website(url: str) -> dict:
    """URL → fetched text → LLM extraction → sane, wizard-ready profile."""
    text = await fetch_website_text(url)
    profile = await _llm_json(_EXTRACT_PROMPT + text)
    return _sane(profile)
