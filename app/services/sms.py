"""Twilio SMS — booking confirmation texts.

When a booking is created (voice agent → ``book_appointment``), we text the
patient a short confirmation. We call Twilio's REST API directly via httpx so
no extra SDK dependency is needed (httpx is already a dependency).

Design rules:
  * **Fail-safe** — SMS is a nice-to-have side effect. A Twilio error must NEVER
    break or roll back a booking. Every public function swallows errors and
    returns a small result dict instead of raising.
  * **Opt-in** — sends only when ``SMS_ENABLED=true`` AND the three Twilio
    settings (account SID, auth token, from-number) are all configured.
  * **No PHI in logs** — we log only the last 4 digits of the destination.
"""

from __future__ import annotations

import logging
import re

import httpx

from app.config import get_settings

logger = logging.getLogger("dentiva.services.sms")

TWILIO_BASE = "https://api.twilio.com"


# --------------------------------------------------------------------------- #
# Phone normalization
# --------------------------------------------------------------------------- #
def normalize_phone(raw: str | None) -> str | None:
    """Best-effort normalize a US phone string to E.164 (+1XXXXXXXXXX).

    Accepts the messy shapes a voice agent collects ("(555) 123-4567",
    "555-123-4567", "5551234567", "+15551234567"). Returns None when there
    aren't enough digits to form a valid number.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}" if len(digits) >= 11 else None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _last4(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if len(digits) >= 4 else "****"


# --------------------------------------------------------------------------- #
# Message body
# --------------------------------------------------------------------------- #
def build_confirmation_body(
    *,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    provider: str | None,
) -> str:
    """Compose the patient-facing confirmation text (kept short — 1 SMS segment)."""
    greeting = f"Hi {first_name}, " if first_name and first_name != "Unknown" else "Hi, "
    with_provider = f" with {provider}" if provider else ""
    return (
        f"{greeting}your appointment at {practice_name} is confirmed for "
        f"{date} at {time}{with_provider}. Reply to this text or call us if you "
        f"need to reschedule."
    )


# --------------------------------------------------------------------------- #
# Low-level send
# --------------------------------------------------------------------------- #
async def send_sms(
    to: str | None,
    body: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Send one SMS via Twilio. Never raises — returns a small result dict.

    Returns one of:
      {"skipped": "<reason>"}            — disabled / not configured / bad number
      {"sent": True, "sid": "<sid>"}     — Twilio accepted the message
      {"error": "<detail>", ...}         — Twilio rejected or network failed
    """
    settings = get_settings()
    if not settings.sms_enabled:
        return {"skipped": "sms_disabled"}
    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    sender = settings.twilio_from_number
    if not (sid and token and sender):
        logger.info("sms: Twilio not fully configured — skipping send")
        return {"skipped": "not_configured"}

    dest = normalize_phone(to)
    if not dest:
        logger.warning("sms: unusable destination number (…%s) — skipping", _last4(to))
        return {"skipped": "bad_number"}

    url = f"{TWILIO_BASE}/2010-04-01/Accounts/{sid}/Messages.json"
    data = {"To": dest, "From": sender, "Body": body}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(url, data=data, auth=(sid, token))
        if resp.status_code >= 400:
            logger.warning(
                "sms: Twilio rejected (status=%s, dest=…%s): %s",
                resp.status_code,
                _last4(dest),
                resp.text[:300],
            )
            return {"error": "twilio_rejected", "status": resp.status_code}
        payload = resp.json()
        logger.info("sms: sent to …%s sid=%s", _last4(dest), payload.get("sid"))
        return {"sent": True, "sid": payload.get("sid")}
    except Exception as exc:  # noqa: BLE001 — fail-safe: SMS must never break booking
        logger.warning("sms: send failed to …%s: %s", _last4(dest), exc)
        return {"error": "send_failed", "detail": str(exc)}
    finally:
        if owns_client:
            await client.aclose()


# --------------------------------------------------------------------------- #
# High-level: booking confirmation
# --------------------------------------------------------------------------- #
async def send_booking_confirmation(
    *,
    to: str | None,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    provider: str | None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build + send a booking confirmation. Fail-safe wrapper around send_sms."""
    body = build_confirmation_body(
        practice_name=practice_name,
        first_name=first_name,
        date=date,
        time=time,
        provider=provider,
    )
    return await send_sms(to, body, client=client)
