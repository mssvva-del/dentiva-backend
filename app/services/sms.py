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
from app.observability.alerts import record_alert

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
def _norm_lang(language: str | None) -> str:
    """Normalize any language tag to the two we support: 'es' or 'en'. Mirrors the
    reactivation copy module so a Spanish caller gets Spanish everywhere."""
    return "es" if (language or "en").lower().startswith("es") else "en"


def build_confirmation_body(
    *,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    provider: str | None,
    language: str = "en",
) -> str:
    """Compose the patient-facing confirmation text (kept short — 1 SMS segment).

    Localized EN/ES — the language comes from the call the booking was made on, so
    a patient who booked in Spanish gets a Spanish confirmation."""
    unknown = not first_name or first_name == "Unknown"
    if _norm_lang(language) == "es":
        greeting = "Hola, " if unknown else f"Hola {first_name}, "
        with_provider = f" con {provider}" if provider else ""
        return (
            f"{greeting}su cita en {practice_name} está confirmada para el "
            f"{date} a las {time}{with_provider}. Responda a este mensaje o "
            f"llámenos si necesita reprogramar."
        )
    greeting = "Hi, " if unknown else f"Hi {first_name}, "
    with_provider = f" with {provider}" if provider else ""
    return (
        f"{greeting}your appointment at {practice_name} is confirmed for "
        f"{date} at {time}{with_provider}. Reply to this text or call us if you "
        f"need to reschedule."
    )


def build_cancellation_body(
    *,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    language: str = "en",
) -> str:
    """Compose the patient-facing cancellation text (kept short — 1 SMS segment)."""
    unknown = not first_name or first_name == "Unknown"
    if _norm_lang(language) == "es":
        greeting = "Hola, " if unknown else f"Hola {first_name}, "
        return (
            f"{greeting}su cita en {practice_name} el {date} a las {time} ha sido "
            f"cancelada. Llámenos cuando quiera para reprogramar — con gusto le "
            f"ayudamos."
        )
    greeting = "Hi, " if unknown else f"Hi {first_name}, "
    return (
        f"{greeting}your appointment at {practice_name} on {date} at {time} has "
        f"been cancelled. Call us anytime to rebook — we're happy to help."
    )


def build_reminder_body(
    *,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    soon: bool,
) -> str:
    """Compose an appointment-reminder text. ``soon`` selects the ~2h wording."""
    greeting = f"Hi {first_name}, " if first_name and first_name != "Unknown" else "Hi, "
    when = "later today" if soon else "tomorrow"
    return (
        f"{greeting}a reminder that your appointment at {practice_name} is {when} "
        f"on {date} at {time}. Reply to this text or call us if you need to "
        f"reschedule. See you soon!"
    )


def build_waitlist_opening_body(
    *,
    practice_name: str,
    first_name: str | None,
    date: str | None = None,
    time: str | None = None,
) -> str:
    """Compose a 'a slot just opened' text for a waitlisted patient.

    A spot freed up (usually from a cancellation). We invite them to call/reply
    to grab it — we don't auto-book, so the slot stays first-come.
    """
    greeting = f"Hi {first_name}, " if first_name and first_name != "Unknown" else "Hi, "
    when = f" on {date} at {time}" if date and time else ""
    return (
        f"{greeting}good news — a spot just opened up at {practice_name}{when}. "
        f"You're on our waitlist, so reply or call us soon to grab it before "
        f"someone else does!"
    )


def build_recall_body(
    *,
    practice_name: str,
    first_name: str | None,
) -> str:
    """Compose a recall / reactivation text for patients overdue for a visit."""
    greeting = f"Hi {first_name}, " if first_name and first_name != "Unknown" else "Hi, "
    return (
        f"{greeting}it's been a while since your last visit to {practice_name}. "
        f"You're due for a check-up — reply or call us and we'll find a time that "
        f"works for you."
    )


# --------------------------------------------------------------------------- #
# Low-level send
# --------------------------------------------------------------------------- #
async def send_sms(
    to: str | None,
    body: str,
    *,
    opted_out: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Send one SMS via Twilio. Never raises — returns a small result dict.

    ``opted_out`` short-circuits the send (TCPA): a patient who texted STOP must
    not receive any further outbound SMS.

    Returns one of:
      {"skipped": "<reason>"}            — disabled / not configured / bad number
      {"sent": True, "sid": "<sid>"}     — Twilio accepted the message
      {"error": "<detail>", ...}         — Twilio rejected or network failed
    """
    if opted_out:
        return {"skipped": "opted_out"}
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
            record_alert("twilio_send_failed", f"status={resp.status_code}")
            return {"error": "twilio_rejected", "status": resp.status_code}
        payload = resp.json()
        logger.info("sms: sent to …%s sid=%s", _last4(dest), payload.get("sid"))
        return {"sent": True, "sid": payload.get("sid")}
    except Exception as exc:  # noqa: BLE001 — fail-safe: SMS must never break booking
        logger.warning("sms: send failed to …%s: %s", _last4(dest), exc)
        record_alert("twilio_send_failed", f"exc={type(exc).__name__}")
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
    opted_out: bool = False,
    language: str = "en",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build + send a booking confirmation. Fail-safe wrapper around send_sms."""
    body = build_confirmation_body(
        practice_name=practice_name,
        first_name=first_name,
        date=date,
        time=time,
        provider=provider,
        language=language,
    )
    return await send_sms(to, body, opted_out=opted_out, client=client)


async def send_cancellation_notice(
    *,
    to: str | None,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    opted_out: bool = False,
    language: str = "en",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build + send a cancellation notice. Fail-safe wrapper around send_sms."""
    body = build_cancellation_body(
        practice_name=practice_name,
        first_name=first_name,
        date=date,
        time=time,
        language=language,
    )
    return await send_sms(to, body, opted_out=opted_out, client=client)


async def send_appointment_reminder(
    *,
    to: str | None,
    practice_name: str,
    first_name: str | None,
    date: str,
    time: str,
    soon: bool,
    opted_out: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build + send an appointment reminder. Fail-safe wrapper around send_sms."""
    body = build_reminder_body(
        practice_name=practice_name,
        first_name=first_name,
        date=date,
        time=time,
        soon=soon,
    )
    return await send_sms(to, body, opted_out=opted_out, client=client)


async def send_recall_notice(
    *,
    to: str | None,
    practice_name: str,
    first_name: str | None,
    opted_out: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build + send a recall / reactivation text. Fail-safe wrapper around send_sms."""
    body = build_recall_body(practice_name=practice_name, first_name=first_name)
    return await send_sms(to, body, opted_out=opted_out, client=client)


async def send_waitlist_opening(
    *,
    to: str | None,
    practice_name: str,
    first_name: str | None,
    date: str | None = None,
    time: str | None = None,
    opted_out: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build + send a waitlist 'slot opened' text. Fail-safe wrapper around send_sms."""
    body = build_waitlist_opening_body(
        practice_name=practice_name,
        first_name=first_name,
        date=date,
        time=time,
    )
    return await send_sms(to, body, opted_out=opted_out, client=client)


async def page_clinic_urgent_callback(
    *,
    to: str | None,
    practice_name: str,
    first_name: str | None,
    patient_phone: str | None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Text the CLINIC that an urgent callback is waiting.

    The row in the dashboard is not a page: the sidebar badge only updates while
    someone has the tab open. An escalated caller has been told the team will
    call back within minutes, so the team has to learn about it without watching
    a screen.

    Minimum necessary PHI: first name + the number to call. The stated reason can
    contain clinical detail, so it stays in the dashboard, not in an SMS.
    """
    body = (
        f"{practice_name}: URGENT callback waiting — "
        f"{first_name or 'a caller'} at {patient_phone or 'number in dashboard'}. "
        "Details in your Dentovox dashboard."
    )
    # opted_out is a patient-consent concept; this goes to the practice's own line.
    return await send_sms(to, body, client=client)
