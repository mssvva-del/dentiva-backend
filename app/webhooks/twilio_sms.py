"""Inbound Twilio SMS webhook — two-way texting.

Patients reply to our outbound texts (reminders, confirmations) and we act on
the keyword:

  * CONFIRM / C / YES   → confirm the soonest upcoming appointment (audit only;
                          the booking is already "confirmed", this records that
                          the *patient* confirmed).
  * CANCEL / X / NO     → cancel the soonest upcoming appointment AND backfill
                          the waitlist (text the next waiting patient).
  * STOP / UNSUBSCRIBE  → opt the patient out of all future SMS (TCPA). Carriers
                          also block at their level; we track it so our senders
                          skip them too.
  * START / UNSTOP      → clear the opt-out.
  * anything else        → a short "reply CONFIRM or CANCEL" help message.

Twilio POSTs application/x-www-form-urlencoded (From, To, Body, …) and expects
a TwiML XML response. Signature validation (X-Twilio-Signature) is enforced
only when ``TWILIO_VALIDATE_SIGNATURE=true`` so the public URL behind a proxy
doesn't have to be reconstructed perfectly in dev.

Fail-safe: any error returns an empty TwiML 200 so Twilio doesn't retry-storm.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert

import app.db as _app_db
from app.config import get_settings
from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.processed_webhook_event import ProcessedWebhookEvent
from app.services.sms import send_cancellation_notice, send_waitlist_opening
from app.webhooks.retell import (
    _backfill_from_waitlist,
    _find_patient_by_phone,
    _find_upcoming_booking,
    _resolve_practice,
)

logger = logging.getLogger("dentiva.webhooks.twilio_sms")

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])

_CONFIRM_WORDS = {"confirm", "c", "yes", "y", "ok", "okay", "confirmed"}
_CANCEL_WORDS = {"cancel", "x", "no", "n"}
_STOP_WORDS = {"stop", "stopall", "unsubscribe", "end", "quit", "cancelall"}
_START_WORDS = {"start", "unstop", "yes-resume"}


def _twiml(message: str | None = None) -> Response:
    """Build a TwiML XML response (optionally with one reply message)."""
    if message:
        # Minimal escaping for the few chars that matter inside XML text.
        safe = (
            message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        body = f"<Response><Message>{safe}</Message></Response>"
    else:
        body = "<Response></Response>"
    return Response(content=body, media_type="application/xml")


def _validate_signature(url: str, params: dict[str, str], signature: str | None) -> bool:
    """Validate Twilio's X-Twilio-Signature per their spec.

    signature == base64(HMAC-SHA1(auth_token, url + sorted(k+v for params))).
    """
    token = get_settings().twilio_auth_token
    if not token or not signature:
        return False
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _classify(body: str) -> str:
    word = body.strip().lower()
    # Take the first token so "C please" / "cancel my appt" still classify.
    first = word.split()[0] if word.split() else word
    if first in _STOP_WORDS:
        return "stop"
    if first in _START_WORDS:
        return "start"
    if first in _CONFIRM_WORDS:
        return "confirm"
    if first in _CANCEL_WORDS:
        return "cancel"
    return "unknown"


@router.post("/status")
async def twilio_status_webhook(request: Request) -> Response:
    """Twilio message status callback (queued/sent/delivered/failed/undelivered).

    Configure as the StatusCallback URL on outbound messages to get delivery
    observability. We log the outcome (no PHI — only the last 4 of the
    destination); failures/undelivered are logged at WARNING so they surface.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    params = dict(parse_qsl(raw))
    sid = params.get("MessageSid", "")
    status = params.get("MessageStatus", "")
    to = params.get("To", "")
    last4 = to[-4:] if len(to) >= 4 else "????"
    if status in ("failed", "undelivered"):
        logger.warning("twilio-status: sid=%s status=%s to=…%s", sid, status, last4)
    else:
        logger.info("twilio-status: sid=%s status=%s to=…%s", sid, status, last4)
    return Response(status_code=204)


@router.post("/sms")
async def twilio_sms_webhook(request: Request) -> Response:
    settings = get_settings()
    # Twilio sends application/x-www-form-urlencoded. Parse the raw body directly
    # so we don't need the python-multipart dependency.
    raw = (await request.body()).decode("utf-8", errors="replace")
    params = dict(parse_qsl(raw))
    from_number = params.get("From", "")
    body = params.get("Body", "")

    if settings.twilio_validate_signature:
        sig = request.headers.get("X-Twilio-Signature")
        if not _validate_signature(str(request.url), params, sig):
            logger.warning("twilio-sms: bad signature — ignoring")
            return _twiml()

    # Idempotency: Twilio redelivers on timeout. Atomically "claim" this MessageSid
    # — if it was already claimed, skip re-processing (else a CONFIRM/CANCEL could
    # double-apply). Claim-first is safe because _handle_sms is fail-safe (always
    # 200), so Twilio only retries on timeout, not on a handled error.
    sid = params.get("MessageSid", "")
    if sid and await _already_claimed(sid):
        logger.info("twilio-sms: duplicate delivery sid=%s — skipping", sid)
        return _twiml()

    intent = _classify(body)
    logger.info("twilio-sms: from=…%s intent=%s", from_number[-4:] if from_number else "??", intent)

    try:
        return await _handle_sms(from_number, intent)
    except Exception:  # noqa: BLE001 — never retry-storm Twilio
        logger.exception("twilio-sms: handler failed")
        return _twiml()


async def _already_claimed(sid: str) -> bool:
    """Record this MessageSid; return True if it was ALREADY recorded (duplicate).

    INSERT … ON CONFLICT DO NOTHING is atomic: the first delivery inserts a row
    (rowcount 1 → not a duplicate), a redelivery conflicts (rowcount 0 → duplicate).
    """
    async with _app_db.async_session_factory() as session:
        result = await session.execute(
            pg_insert(ProcessedWebhookEvent)
            .values(id=uuid.uuid4(), source="twilio_sms", event_id=sid)
            .on_conflict_do_nothing(index_elements=["source", "event_id"])
        )
        await session.commit()
    return result.rowcount == 0


async def _handle_sms(from_number: str, intent: str) -> Response:
    practice = await _resolve_practice(None)
    if practice is None:
        return _twiml()
    practice_id = practice.id
    practice_name = practice.name

    async with _app_db.async_session_factory() as session:
        await set_tenant(session, practice_id)
        patient = await _find_patient_by_phone(session, practice_id, from_number)
        if patient is None:
            # Unknown number — acknowledge politely, do nothing.
            return _twiml(
                "Thanks for texting! We couldn't match your number to a record. "
                "Please call the office and we'll be glad to help."
            )

        first_name = patient.first_name or "there"

        if intent == "stop":
            patient.sms_opt_out = True
            session.add(_audit(practice_id, "sms_opt_out", patient.id))
            await session.commit()
            return _twiml(
                "You're unsubscribed and won't get further texts. Reply START to "
                "opt back in anytime."
            )

        if intent == "start":
            patient.sms_opt_out = False
            session.add(_audit(practice_id, "sms_opt_in", patient.id))
            await session.commit()
            return _twiml(f"Welcome back, {first_name}! You'll receive our texts again.")

        booking = await _find_upcoming_booking(session, practice_id, patient.id)

        if intent == "confirm":
            if booking is None:
                await session.commit()
                return _twiml(
                    "Thanks! We don't see an upcoming appointment on file — call us "
                    "to book one anytime."
                )
            session.add(
                _audit(
                    practice_id,
                    "booking_confirmed_sms",
                    booking.id,
                    resource_type="booking",
                    extra={"appointment_at": booking.appointment_at.isoformat()},
                )
            )
            await session.commit()
            when = booking.appointment_at.strftime("%b %-d at %-I:%M %p")
            return _twiml(
                f"Thanks {first_name}, your appointment on {when} is confirmed. "
                "See you then!"
            )

        if intent == "cancel":
            if booking is None:
                await session.commit()
                return _twiml(
                    "We don't see an upcoming appointment to cancel. Call us if you "
                    "need anything!"
                )
            cancelled_date = booking.appointment_at.date().isoformat()
            cancelled_time = booking.appointment_at.strftime("%H:%M")
            booking.status = "cancelled"
            session.add(
                _audit(
                    practice_id,
                    "booking_cancelled",
                    booking.id,
                    resource_type="booking",
                    extra={
                        "via": "sms",
                        "appointment_at": booking.appointment_at.isoformat(),
                    },
                )
            )
            # Backfill the waitlist (fail-safe).
            notify_target = None
            try:
                notify_target = await _backfill_from_waitlist(
                    session, practice_id, retell_call_id="sms"
                )
            except Exception:  # noqa: BLE001
                logger.exception("twilio-sms: waitlist backfill failed")
            patient_opted_out = patient.sms_opt_out
            await session.commit()

            # Confirmation of cancellation (the inbound text already proves consent
            # to receive this one; opt-out still respected).
            await send_cancellation_notice(
                to=from_number,
                practice_name=practice_name,
                first_name=first_name,
                date=cancelled_date,
                time=cancelled_time,
                opted_out=patient_opted_out,
            )
            if notify_target is not None:
                wl_first, wl_phone, wl_opted = notify_target
                await send_waitlist_opening(
                    to=wl_phone,
                    practice_name=practice_name,
                    first_name=wl_first,
                    date=cancelled_date,
                    time=cancelled_time,
                    opted_out=wl_opted,
                )
            return _twiml(
                f"Done, {first_name} — your appointment is cancelled. Call us "
                "anytime to rebook. Take care!"
            )

        # Unknown keyword.
        await session.commit()
        return _twiml(
            "Reply CONFIRM to keep your appointment, CANCEL to cancel, or STOP to "
            "unsubscribe. For anything else, please call the office."
        )


def _audit(
    practice_id: uuid.UUID,
    action: str,
    resource_id: uuid.UUID,
    *,
    resource_type: str = "patient",
    extra: dict | None = None,
) -> AuditLog:
    meta = {"via": "sms"}
    if extra:
        meta.update(extra)
    return AuditLog(
        id=uuid.uuid4(),
        practice_id=practice_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        audit_metadata=meta,
    )
