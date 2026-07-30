"""Retell webhook handler — Phase 2.

Handles:
  * call_started  — upsert a ``calls`` row (idempotent via retell_call_id UNIQUE).
  * call_ended    — update status, duration, transcript, outcome.
  * function_call book_appointment — return mock slots + create ``bookings`` +
                                     write ``audit_logs``.

Auth: ``X-Retell-Signature`` HMAC over the raw body, compared to
RETELL_WEBHOOK_SECRET. When the secret is empty (local dev), verification is
skipped with a warning.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

import app.db as _app_db
from app.billing.metering import record_call_usage
from app.config import get_settings
from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.callback_request import CallbackRequest
from app.models.enums import AI_CALL, CANCELLED, CONFIRMED
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.waitlist_entry import WaitlistEntry
from app.observability.alerts import record_alert
from app.services.availability import compute_native_slots, slot_to_utc
from app.services.call_outcome import BOOKED, classify_outcome
from app.services.sms import (
    send_booking_confirmation,
    send_cancellation_notice,
    send_waitlist_opening,
)
from app.utils.crypto import phone_hmac

logger = logging.getLogger("dentiva.webhooks.retell")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Fire confirmation/cancellation SMS WITHOUT blocking the live agent's tool
# response — Twilio can take up to 15s, which is dead air on the call. The send
# functions are self-contained (no request session) and fail-safe, so a detached
# task is safe. Keep a reference so the task isn't garbage-collected mid-flight.
_bg_sms_tasks: set = set()


def _fire_sms(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_sms_tasks.add(task)
    task.add_done_callback(_bg_sms_tasks.discard)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


# Retell's signature scheme (matches retell-sdk `verify`): the X-Retell-Signature
# header is "v={unix_ms},d={hex}" where hex = HMAC-SHA256(secret, raw_body + str(ms)).
# The secret is the API key that has the "webhook" badge in the Retell dashboard.
# NOTE: our old code compared a plain HMAC over the body against the WHOLE header
# string → it never matched, so a set secret produced 401 on every real webhook
# (it only "worked" while the secret was unset and the check was skipped).
_RETELL_SIG_RE = re.compile(r"v=(\d+),d=(.+)")
_RETELL_TOLERANCE_MS = 5 * 60 * 1000  # reject signatures older/newer than 5 min


def _verify_signature(
    raw_body: bytes, signature: str | None, *, now_ms: int | None = None
) -> bool:
    settings = get_settings()
    secret = settings.retell_webhook_secret
    if not secret:
        # No secret → we cannot verify. In prod, FAIL CLOSED: reject rather than
        # process a forgeable book/cancel/transfer. (verify_security_config already
        # refuses to boot prod without this secret; this is the second guard so the
        # two can never drift into an open hole.) Dev keeps working for local tests.
        if settings.environment == "production":
            logger.error("RETELL_WEBHOOK_SECRET NOT set in production — rejecting webhook.")
            return False
        logger.warning("RETELL_WEBHOOK_SECRET empty — skipping signature check (dev).")
        return True
    if not signature:
        return False
    match = _RETELL_SIG_RE.search(signature)
    if not match:
        return False
    poststamp = int(match.group(1))
    post_digest = match.group(2)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now - poststamp) > _RETELL_TOLERANCE_MS:
        return False  # replay / clock-skew guard
    # HMAC over raw_body ++ the timestamp string (byte-exact with the SDK, which
    # signs (body_str + str(ms)).encode()).
    signed = raw_body + str(poststamp).encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, post_digest)


# ---------------------------------------------------------------------------
# Emergency lock — PROGRAMMATIC, not prompt-based.
#
# The LLM prompt can be talked out of "emergency mode" under conversational
# pressure (this happened in a real test call). So the lock lives HERE, in the
# tool router, and the on/off state is PERSISTED in calls.emergency_active so it
# survives backend restarts and spans every webhook/tool call within one phone
# call.
#
# DOUBLE TRIGGER (either one flips the flag — we never rely on the LLM alone):
#   (a) the LLM called create_callback_request(urgent=true), OR
#   (b) backend itself scans the args of ANY tool call for emergency keywords
#       (deterministic regex — independent of whether the LLM flagged urgent).
#
# Once emergency_active is True, check_availability and book_appointment are
# physically refused and a human-readable phrase is returned for the agent to
# speak. Only create_callback_request and transfer_to_human stay allowed.
# ---------------------------------------------------------------------------

# Deterministic keyword scan. Substring patterns (not \b word boundaries) so
# inflections match: bleed/bleeding/bleeds, swell/swelling, breath/breathing/
# breathe, knocked out / knocked-out / knockedout, uncontrolled/uncontrollable.
_EMERGENCY_PATTERNS = (
    r"bleed",
    r"swell",
    r"swollen",
    r"severe\s+pain",
    r"uncontroll",
    r"breath",
    r"knocked[\s-]?out",
    r"emergency",
    r"urgent",
)
_EMERGENCY_RE = re.compile("|".join(_EMERGENCY_PATTERNS), re.IGNORECASE)

# Tools that schedule and must be refused while an emergency is active.
# reschedule is scheduling too; cancel stays allowed (it only frees up time).
_SCHEDULING_TOOLS = frozenset(
    {"check_availability", "book_appointment", "reschedule_appointment"}
)

_EMERGENCY_BLOCK_MESSAGE = (
    "I can't schedule a regular appointment while we're handling your urgent "
    "situation. Our team is being notified right now and will call you "
    "immediately."
)


def _is_truthy(value) -> bool:
    """Tolerant truthiness — Retell may send a bool, a string, or a number."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "y")
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _args_to_text(args: dict) -> str:
    """Flatten arg values into one string for keyword scanning."""
    parts: list[str] = []
    for value in args.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value is not None and not isinstance(value, (dict, bool)):
            parts.append(str(value))
    return " ".join(parts)


def _contains_emergency_keywords(args: dict) -> bool:
    return bool(_EMERGENCY_RE.search(_args_to_text(args)))


# ---------------------------------------------------------------------------
# Helper — look up practice by retell_agent_id or fall back to the first seed
# ---------------------------------------------------------------------------


async def _resolve_practice(agent_id: str | None) -> Practice | None:
    """Resolve which practice a call belongs to from its Retell ``agent_id``.

    Routing rule (HIPAA-safe — never silently attribute a call to the wrong
    clinic):
      * ``agent_id`` matches a ``practices.retell_agent_id`` → that practice.
      * ``agent_id`` is None or unmatched → fall back to the only practice in
        the DB ONLY when exactly one exists (single-tenant / weekend mode).
        With 2+ practices the tenant is ambiguous, so we REFUSE (return None)
        and log critically rather than leak one clinic's call into another's
        records.

    The previous implementation always fell back to ``SELECT ... LIMIT 1``,
    which once a second clinic onboards routes every unmatched call to whichever
    practice sorts first — a cross-tenant PHI leak. Closing that requires each
    practice to carry its own ``retell_agent_id``.
    """
    async with _app_db.async_session_factory() as session:
        if agent_id:
            result = await session.execute(
                select(Practice).where(Practice.retell_agent_id == agent_id)
            )
            practice = result.scalar_one_or_none()
            if practice:
                return practice

        # No match: only safe to guess in the unambiguous single-practice case.
        rows = (await session.execute(select(Practice).limit(2))).scalars().all()
        if len(rows) == 1:
            return rows[0]
        if not rows:
            return None
        logger.critical(
            "resolve_practice: REFUSING to route — agent_id=%r matched no "
            "practice and %d practices exist. Set practices.retell_agent_id for "
            "each clinic (its Retell agent id). Returning None to avoid a "
            "cross-tenant data leak.",
            agent_id,
            len(rows),
        )
        return None


# ---------------------------------------------------------------------------
# Helper — upsert / look up a patient from voice args
# ---------------------------------------------------------------------------


async def _upsert_patient(
    session,
    practice_id: uuid.UUID,
    first_name: str,
    last_name: str,
    phone: str,
    language: str | None = None,
) -> Patient:
    """Return existing patient by phone or create a stub for this call.

    ``language`` is the language this call was conducted in. For a NEW patient we
    store it as preferred_language so downstream SMS + reactivation speak their
    language. We do NOT overwrite an existing patient's stored preference (a
    relative may call in a different language)."""
    # Indexed lookup by the deterministic phone hash — no full-table scan / decrypt.
    existing = await _find_patient_by_phone(session, practice_id, phone)
    if existing is not None:
        return existing

    # Create a new stub patient. Only 'es'/'en' supported; default 'en'.
    preferred_language = "es" if (language or "").lower().startswith("es") else "en"
    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=f"VOICE-{uuid.uuid4().hex[:8].upper()}",
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        preferred_language=preferred_language,
    )
    session.add(patient)
    await session.flush()
    return patient


async def _find_patient_by_phone(
    session, practice_id: uuid.UUID, phone: str
) -> Patient | None:
    """Read-only lookup of a patient by phone within a practice — indexed via the
    deterministic phone hash (no scan/decrypt).

    Deterministic on collision: family members can share one phone in a practice.
    We return the OLDEST match (stable) and log when more than one exists so the
    ambiguity is visible rather than silently picking a random row."""
    h = phone_hmac(phone)
    if not h:
        return None
    rows = (
        await session.execute(
            select(Patient)
            .where(Patient.practice_id == practice_id, Patient.phone_hmac == h)
            .order_by(Patient.created_at.asc())
            .limit(2)
        )
    ).scalars().all()
    if len(rows) > 1:
        logger.warning(
            "phone lookup: %d patients share this number in practice %s — using oldest",
            len(rows), practice_id,
        )
    return rows[0] if rows else None


async def _find_upcoming_booking(
    session, practice_id: uuid.UUID, patient_id: uuid.UUID
) -> Booking | None:
    """Return the soonest upcoming confirmed booking for a patient, if any."""
    now = datetime.now(tz=UTC)
    result = await session.execute(
        select(Booking)
        .where(
            Booking.practice_id == practice_id,
            Booking.patient_id == patient_id,
            Booking.status == "confirmed",
            Booking.appointment_at >= now,
        )
        .order_by(Booking.appointment_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_practice_id_for_call(session, retell_call_id: str) -> uuid.UUID | None:
    """Resolve the practice for a call row, falling back to the first practice.

    `calls` is RLS-protected, but this runs BEFORE we know the tenant (routing).
    We use the SECURITY DEFINER routing function to look up only the practice_id
    (no PHI) without being blocked by RLS, then the caller binds the tenant.
    """
    practice_id = (
        await session.execute(
            text("SELECT dentiva_practice_for_retell_call(:cid)"),
            {"cid": retell_call_id},
        )
    ).scalar()
    if practice_id is not None:
        return practice_id
    resolved = await _resolve_practice(None)
    return resolved.id if resolved else None


# ---------------------------------------------------------------------------
# call_started handler
# ---------------------------------------------------------------------------


async def _resolve_practice_meta(call_data: dict, agent_id: str | None) -> Practice | None:
    """Prefer an explicit ``metadata.practice_id`` (the web-call demo carries it,
    since every web call shares the demo agent and agent_id can't disambiguate),
    else fall back to agent-id routing."""
    pid = (call_data.get("metadata") or {}).get("practice_id")
    if pid:
        try:
            async with _app_db.async_session_factory() as session:
                p = (await session.execute(
                    select(Practice).where(Practice.id == uuid.UUID(str(pid)))
                )).scalar_one_or_none()
                if p:
                    return p
        except ValueError:
            pass
    return await _resolve_practice(agent_id)


async def _ensure_call_row(retell_call_id: str, call_obj: dict) -> None:
    """Make sure a calls row exists BEFORE a tool call is handled.

    A web call can invoke book_appointment before call_started's row is committed
    (or call_started may not fire at all), and every tool routes the tenant via
    the call row — no row + 2 practices → routing refuses and the booking is
    silently DROPPED. Create a minimal row from the tool payload's
    metadata.practice_id so the booking/patient attach to the right clinic. If
    call_started races us, the retell_call_id UNIQUE conflict is harmless.
    """
    if not retell_call_id:
        return
    pid = (call_obj.get("metadata") or {}).get("practice_id")
    if not pid:
        return
    try:
        practice_uuid = uuid.UUID(str(pid))
    except ValueError:
        return
    async with _app_db.async_session_factory() as session:
        await set_tenant(session, practice_uuid)
        exists = (await session.execute(
            select(Call.id).where(Call.retell_call_id == retell_call_id)
        )).scalar_one_or_none()
        if exists:
            return
        session.add(Call(
            id=uuid.uuid4(), practice_id=practice_uuid, retell_call_id=retell_call_id,
            direction=call_obj.get("direction") or "inbound",
            from_number=call_obj.get("from_number") or "web",
            to_number=call_obj.get("to_number") or "web",
            started_at=datetime.now(tz=UTC), status="in_progress",
        ))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # call_started inserted first — fine


async def _handle_call_started(payload: dict) -> dict:
    retell_call_id = payload.get("call_id") or payload.get("retell_call_id", "")
    call_data = payload.get("call", {}) or {}
    agent_id = call_data.get("agent_id") or payload.get("agent_id")
    from_number = call_data.get("from_number") or payload.get("from_number", "unknown")
    to_number = call_data.get("to_number") or payload.get("to_number", "unknown")
    started_at_raw = call_data.get("start_timestamp") or payload.get("start_timestamp")

    if started_at_raw:
        if isinstance(started_at_raw, (int, float)):
            started_at = datetime.fromtimestamp(started_at_raw / 1000, tz=UTC)
        else:
            started_at = datetime.fromisoformat(str(started_at_raw))
    else:
        started_at = datetime.now(tz=UTC)

    practice = await _resolve_practice_meta(call_data, agent_id)
    if practice is None:
        logger.warning("call_started: no practice found, ignoring. call_id=%s", retell_call_id)
        return {"ok": True, "warning": "no_practice"}

    async with _app_db.async_session_factory() as session:
        # calls is RLS-protected — bind the resolved practice so the INSERT passes
        # the tenant policy (FORCE RLS checks practice_id against the GUC).
        await set_tenant(session, practice.id)
        stmt = (
            pg_insert(Call)
            .values(
                id=uuid.uuid4(),
                practice_id=practice.id,
                retell_call_id=retell_call_id,
                direction="inbound",
                from_number=from_number,
                to_number=to_number,
                started_at=started_at,
                status="in_progress",
            )
            .on_conflict_do_nothing(index_elements=["retell_call_id"])
        )
        await session.execute(stmt)
        await session.commit()

    logger.info("call_started persisted: retell_call_id=%s", retell_call_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# call_ended handler
# ---------------------------------------------------------------------------


async def _handle_call_ended(payload: dict) -> dict:
    retell_call_id = payload.get("call_id") or payload.get("retell_call_id", "")
    call_data = payload.get("call", {}) or {}

    end_ts = call_data.get("end_timestamp") or payload.get("end_timestamp")
    start_ts = call_data.get("start_timestamp") or payload.get("start_timestamp")

    if end_ts:
        if isinstance(end_ts, (int, float)):
            ended_at = datetime.fromtimestamp(end_ts / 1000, tz=UTC)
        else:
            ended_at = datetime.fromisoformat(str(end_ts))
    else:
        ended_at = datetime.now(tz=UTC)

    duration_seconds: int | None = None
    if end_ts and start_ts:
        if isinstance(end_ts, (int, float)) and isinstance(start_ts, (int, float)):
            duration_seconds = int((end_ts - start_ts) / 1000)

    # Retell transcript format: list of dicts with role + content.
    transcript_raw = call_data.get("transcript") or payload.get("transcript")
    transcript_jsonb: list | None = None
    if isinstance(transcript_raw, list):
        transcript_jsonb = transcript_raw
    elif isinstance(transcript_raw, str) and transcript_raw.strip():
        # Sometimes Retell returns a plain string; store as-is inside a list.
        transcript_jsonb = [{"role": "raw", "content": transcript_raw}]

    disconnection_reason = (
        call_data.get("disconnection_reason") or payload.get("disconnection_reason", "")
    )
    if disconnection_reason in ("user_hangup", "agent_hangup", ""):
        call_status = "completed"
    else:
        call_status = "missed"

    # Resolve the practice from the agent FIRST: calls/bookings are now RLS-
    # protected, so we must bind the tenant before reading or writing them. The
    # call belongs to the same practice that call_started used (same agent_id).
    agent_id = call_data.get("agent_id") or payload.get("agent_id")
    resolved_practice = await _resolve_practice_meta(call_data, agent_id)

    async with _app_db.async_session_factory() as session:
        if resolved_practice is not None:
            await set_tenant(session, resolved_practice.id)
        # FOR UPDATE serializes concurrent call_ended redeliveries on this row, so
        # the meter-once guard below (usage_metered_at IS NULL) can't be read as
        # None by two handlers at once and double-count the minutes.
        result = await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id).with_for_update()
        )
        call = result.scalar_one_or_none()
        if call is None:
            logger.warning(
                "call_ended: no call row for retell_call_id=%s; creating one.", retell_call_id
            )
            # Orphan row — use the practice resolved above (tenant already bound).
            practice = resolved_practice
            practice_id = practice.id if practice else uuid.uuid4()
            from_number = call_data.get("from_number") or payload.get("from_number", "unknown")
            to_number = call_data.get("to_number") or payload.get("to_number", "unknown")
            started_at = ended_at  # best-effort
            call = Call(
                id=uuid.uuid4(),
                practice_id=practice_id,
                retell_call_id=retell_call_id,
                direction="inbound",
                from_number=from_number,
                to_number=to_number,
                started_at=started_at,
                status=call_status,
            )
            session.add(call)
            await session.flush()

        call.ended_at = ended_at
        if duration_seconds is not None:
            call.duration_seconds = duration_seconds
        if transcript_jsonb is not None:
            call.transcript_jsonb = transcript_jsonb

        # Extract recording URL from Retell payload
        recording_url = call_data.get("recording_url") or payload.get("recording_url")
        if recording_url:
            call.recording_path = recording_url  # stored in recording_path column

        # Extract detected language from Retell payload
        language = call_data.get("detected_language") or call_data.get("language")
        if language:
            call.language_detected = language

        # Classify the outcome from every signal we have at hang-up. intent /
        # escalation may already be present if call_analyzed arrived first (event
        # order isn't guaranteed); call_analyzed re-runs this once they're known.
        booking_result = await session.execute(
            select(Booking.id).where(Booking.source_call_id == call.id)
        )
        booking_exists = booking_result.scalar_one_or_none() is not None
        call.outcome = classify_outcome(
            booking_exists=booking_exists,
            call_status=call_status,
            disconnection_reason=disconnection_reason,
            duration_seconds=call.duration_seconds,
            escalation_needed=call.escalation_needed,
            call_intent=call.call_intent,
        )
        call.status = call_status

        # Billing metering (Phase D): a completed call adds its minutes to the
        # practice's current-period usage. Only count answered calls (a 'missed'
        # call consumed no agent minutes). METER ONCE — Retell redelivers
        # call_ended on timeout, so we stamp usage_metered_at and skip if already
        # set (else a retry would double-count minutes). Best-effort — never fail
        # the webhook over metering, but log loudly so under-billing is visible.
        if call_status == "completed" and call.usage_metered_at is None:
            try:
                await record_call_usage(session, call.practice_id, duration_seconds)
                call.usage_metered_at = ended_at
            except Exception:  # noqa: BLE001
                logger.exception(
                    "metering failed for call %s (practice %s)",
                    retell_call_id, call.practice_id,
                )
                # Silent under-billing otherwise — page us (ids only, no PHI).
                record_alert("metering_failed", f"practice={call.practice_id}")

        await session.commit()

    logger.info("call_ended persisted: retell_call_id=%s status=%s", retell_call_id, call_status)
    return {"ok": True}


# ---------------------------------------------------------------------------
# call_analyzed handler
# ---------------------------------------------------------------------------


async def _handle_call_analyzed(payload: dict) -> dict:
    """Store Retell post-call analysis results on the calls row."""
    retell_call_id = payload.get("call_id") or payload.get("retell_call_id", "")
    analysis = payload.get("call_analysis") or payload.get("analysis") or {}
    # Retell puts our configured post-call questions (config.yaml) under
    # ``custom_analysis_data`` — reading them off the top level (the old code) meant
    # these columns were ALWAYS null and the dashboard rendered blanks. Prefer the
    # custom bucket; fall back to top-level for the standard fields / older payloads.
    custom = analysis.get("custom_analysis_data") or {}

    def _field(key: str, *alts: str):
        for src in (custom, analysis):
            for k in (key, *alts):
                if k in src and src[k] is not None:
                    return src[k]
        return None

    call_intent = _field("intent")                                   # enum string
    patient_sentiment = _field("patient_sentiment", "user_sentiment")  # enum string
    escalation_needed = _field("escalation_needed")                  # bool
    hipaa_compliant = _field("hipaa_compliant")                      # bool

    async with _app_db.async_session_factory() as session:
        # calls is RLS-protected — resolve + bind tenant before the lookup.
        routed_practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if routed_practice_id is not None:
            await set_tenant(session, routed_practice_id)
        result = await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )
        call = result.scalar_one_or_none()
        if call is None:
            logger.warning("call_analyzed: no call row for retell_call_id=%s", retell_call_id)
            return {"ok": True, "warning": "call_not_found"}

        if call_intent is not None:
            call.call_intent = call_intent
        if patient_sentiment is not None:
            call.patient_sentiment = patient_sentiment
        if escalation_needed is not None:
            call.escalation_needed = escalation_needed
        if hipaa_compliant is not None:
            call.hipaa_compliant = hipaa_compliant

        # Refine the outcome now that intent/escalation are known — this is what
        # separates a lost booking (no_booking) or emergency from a plain
        # info_only that call_ended could only guess at. Never downgrade a booking.
        if call.outcome != BOOKED:
            booking_exists = (await session.execute(
                select(Booking.id).where(Booking.source_call_id == call.id)
            )).scalar_one_or_none() is not None
            call.outcome = classify_outcome(
                booking_exists=booking_exists,
                call_status=call.status,
                duration_seconds=call.duration_seconds,
                escalation_needed=call.escalation_needed,
                call_intent=call.call_intent,
            )

        await session.commit()

    logger.info(
        "call_analyzed stored: retell_call_id=%s intent=%s sentiment=%s",
        retell_call_id,
        call_intent,
        patient_sentiment,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# book_appointment function_call handler
# ---------------------------------------------------------------------------


async def _handle_check_availability(retell_call_id: str, args: dict) -> dict:
    """Offer REAL openings from the clinic's own hours minus its booked slots.

    No live PMS is required — this reads business_hours + our bookings table, so
    the agent never invents times. (When a clinic connects a real PMS later, that
    adapter path can be branched in here.)"""
    async with _app_db.async_session_factory() as session:
        pid = await _resolve_practice_id_for_call(session, retell_call_id)
        if pid is None:
            return {"available_slots": []}
        await set_tenant(session, pid)
        practice = (
            await session.execute(select(Practice).where(Practice.id == pid))
        ).scalar_one_or_none()
        if practice is None:
            return {"available_slots": []}
        slots = await compute_native_slots(
            session, practice,
            procedure=args.get("procedure", "cleaning"),
            preferred_date=args.get("preferred_date", ""),
            preferred_window=args.get("preferred_time_window"),
        )
    return {
        "available_slots": [
            {"date": s.date, "time": s.time, "provider": s.provider} for s in slots
        ]
    }


async def _handle_book_appointment(retell_call_id: str, args: dict) -> dict:
    # Phase 2: persist a real booking row + audit log.
    first_name = args.get("patient_first_name", "Unknown")
    last_name = args.get("patient_last_name", "")
    phone = args.get("patient_phone", "")
    preferred_date = args.get("preferred_date", "")
    procedure = args.get("procedure", "cleaning")

    # Chosen slot is selected inside the session (double-book guard needs the DB).
    chosen_slot = None

    async with _app_db.async_session_factory() as session:
        # calls/bookings are RLS-protected. Resolve the tenant FIRST (routing fn,
        # bypasses RLS for the practice_id only) and bind it, so the Call lookup
        # below actually sees the row instead of falling into the orphan branch.
        routed_practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if routed_practice_id is not None:
            await set_tenant(session, routed_practice_id)

        # Resolve call row (may or may not exist depending on call_started timing).
        result = await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )
        call = result.scalar_one_or_none()

        practice_id: uuid.UUID
        call_internal_id: uuid.UUID | None = None

        resolved_practice: Practice | None = None
        if call:
            practice_id = call.practice_id
            call_internal_id = call.id
        else:
            resolved_practice = await _resolve_practice(None)
            if resolved_practice is None:
                # No practice in DB — can't persist (dev/test edge case).
                logger.warning(
                    "book_appointment: no practice found; cannot persist."
                )
                return {"result": {"available_slots": []}}
            practice_id = resolved_practice.id

        # Set tenant context so RLS allows patient inserts.
        await set_tenant(session, practice_id)

        # Idempotency: a redelivered book_appointment must NOT create a second
        # booking. If this call already produced a confirmed booking, return it.
        if call_internal_id is not None:
            existing = (
                await session.execute(
                    select(Booking).where(
                        Booking.source_call_id == call_internal_id,
                        Booking.status == "confirmed",
                    )
                )
            ).scalars().first()
            if existing is not None:
                ts = existing.appointment_at
                return {
                    "booked": True,
                    "appointment": {
                        "date": ts.date().isoformat(),
                        "time": ts.strftime("%H:%M"),
                        "provider": existing.provider_name or "Dr. Smith",
                        "procedure": existing.procedure_type or procedure,
                    },
                    "message": "That appointment is already booked.",
                    "available_slots": [],
                }

        # Full practice for name + timezone + native availability.
        practice_obj = resolved_practice or (
            await session.execute(select(Practice).where(Practice.id == practice_id))
        ).scalar_one_or_none()
        practice_name = (practice_obj.name if practice_obj else None) or "our office"

        # Language this call was conducted in — the agent passes it, falling back
        # to Retell's mid-call detection on the call row. Drives preferred_language
        # for the new patient and the confirmation SMS language.
        call_language = args.get("language") or (call.language_detected if call else None)

        # Upsert patient (stores preferred_language on a new patient).
        patient = await _upsert_patient(
            session, practice_id, first_name, last_name, phone, language=call_language
        )
        patient_opted_out = patient.sms_opt_out

        # REAL openings from the clinic's own hours minus booked slots (same source
        # check_availability offered). Honor an exact time if the agent passed one,
        # else take the first free slot in the requested window.
        wanted_time = (args.get("preferred_time") or "").strip()
        slots = []
        if practice_obj is not None:
            slots = await compute_native_slots(
                session, practice_obj,
                procedure=procedure,
                preferred_date=preferred_date,
                preferred_window=args.get("preferred_time_window"),
            )
        if wanted_time:
            chosen_slot = next((s for s in slots if s.time == wanted_time), None)
        if chosen_slot is None:
            chosen_slot = slots[0] if slots else None
        if chosen_slot is None:
            # Nothing real is open for that request — never invent a time.
            return {
                "booked": False,
                "message": (
                    "I'm sorry — I don't see an opening that fits. "
                    "Want me to check another day, or have the team call you?"
                ),
                "available_slots": [],
            }

        # Create booking row — store the LOCAL slot converted to UTC.
        appointment_at = slot_to_utc(
            chosen_slot.date, chosen_slot.time,
            practice_obj.timezone if practice_obj else None,
        )
        booking = Booking(
            id=uuid.uuid4(),
            practice_id=practice_id,
            patient_id=patient.id,
            source_call_id=call_internal_id,
            appointment_at=appointment_at,
            duration_minutes=60,
            procedure_type=procedure,
            provider_name=chosen_slot.provider if chosen_slot else "Dr. Smith",
            status=CONFIRMED,
            source=AI_CALL,
        )
        try:
            # Savepoint so a collision keeps the patient upsert; the partial-unique
            # indexes (uq_bookings_practice_slot_confirmed / _source_call_confirmed)
            # are the real double-book guard — this just turns a race into a clean
            # answer instead of a 500 (dead air on a live call).
            async with session.begin_nested():
                session.add(booking)
        except IntegrityError:
            # Another caller (or a redelivery of THIS call) booked first.
            dup = None
            if call_internal_id is not None:
                dup = (await session.execute(select(Booking).where(
                    Booking.source_call_id == call_internal_id,
                    Booking.status == "confirmed",
                ))).scalars().first()
            if dup is not None:  # this call already has a booking → idempotent success
                ts = dup.appointment_at
                return {
                    "booked": True,
                    "appointment": {
                        "date": ts.date().isoformat(), "time": ts.strftime("%H:%M"),
                        "provider": dup.provider_name or "Dr. Smith",
                        "procedure": dup.procedure_type or procedure,
                    },
                    "message": "That appointment is already booked.",
                    "available_slots": [],
                }
            others = [s for s in slots
                      if not (s.date == chosen_slot.date and s.time == chosen_slot.time)]
            return {
                "booked": False,
                "message": "I'm sorry — that time was just taken. Want one of these instead?",
                "available_slots": [
                    {"date": s.date, "time": s.time, "provider": s.provider} for s in others
                ],
            }

        # Update call outcome if we have the call row.
        if call:
            call.outcome = BOOKED

        # Write audit log.
        audit = AuditLog(
            id=uuid.uuid4(),
            practice_id=practice_id,
            action="booking_created",
            resource_type="booking",
            resource_id=booking.id,
            audit_metadata={
                "retell_call_id": retell_call_id,
                "procedure": procedure,
                "preferred_date": preferred_date,
                "patient_phone_last4": phone[-4:] if len(phone) >= 4 else "****",
            },
        )
        session.add(audit)
        await session.commit()

    logger.info(
        "book_appointment: booking created. retell_call_id=%s booking_id=%s",
        retell_call_id,
        booking.id,
    )

    # Fire the confirmation SMS — fail-safe, never blocks/raises into the booking.
    # Use the ACTUAL booked slot (date + time), not the raw request.
    booked_date = chosen_slot.date
    booked_time = chosen_slot.time
    booked_provider = chosen_slot.provider
    _fire_sms(send_booking_confirmation(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=booked_date,
        time=booked_time,
        provider=booked_provider,
        opted_out=patient_opted_out,
        language=patient.preferred_language,
    ))

    return {
        "booked": True,
        "appointment": {
            "date": booked_date,
            "time": booked_time,
            "provider": booked_provider,
            "procedure": procedure,
        },
        "message": (
            f"Appointment confirmed for {first_name} on {booked_date} "
            f"at {booked_time} with {booked_provider}."
        ),
        "available_slots": [],
    }


# ---------------------------------------------------------------------------
# reschedule_appointment function_call handler
# ---------------------------------------------------------------------------


async def _handle_reschedule_appointment(retell_call_id: str, args: dict) -> dict:
    """Move a patient's upcoming appointment to a new date/slot.

    Identifies the booking by the caller's phone (soonest upcoming confirmed
    one). Re-checks availability for the requested date and shifts the booking,
    writes an audit log, and texts an updated confirmation.
    """
    phone = args.get("patient_phone", "")
    new_date = args.get("new_date", "")
    new_window = args.get("new_time_window")

    async with _app_db.async_session_factory() as session:
        practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if practice_id is None:
            return {
                "rescheduled": False,
                "message": (
                    "I'm having trouble accessing the schedule — let me "
                    "have our team call you back."
                ),
            }
        await set_tenant(session, practice_id)

        patient = await _find_patient_by_phone(session, practice_id, phone)
        if patient is None:
            return {
                "rescheduled": False,
                "message": (
                    "I couldn't find an appointment under that number. "
                    "Would you like to book a new one?"
                ),
            }
        booking = await _find_upcoming_booking(session, practice_id, patient.id)
        if booking is None:
            return {
                "rescheduled": False,
                "message": (
                    "I don't see an upcoming appointment to move. "
                    "Would you like to book one?"
                ),
            }

        procedure = booking.procedure_type or "cleaning"
        practice_obj = (
            await session.execute(select(Practice).where(Practice.id == practice_id))
        ).scalar_one_or_none()
        slots = []
        if practice_obj is not None:
            slots = await compute_native_slots(
                session, practice_obj,
                procedure=procedure,
                preferred_date=new_date,
                preferred_window=new_window,
            )
        wanted_time = (args.get("preferred_time") or "").strip()
        chosen = None
        if wanted_time:
            chosen = next((s for s in slots if s.time == wanted_time), None)
        if chosen is None:
            chosen = slots[0] if slots else None
        if chosen is None:
            return {
                "rescheduled": False,
                "message": (
                    f"I don't see an opening on {new_date}. "
                    "Want me to check another day?"
                ),
                "available_slots": [],
            }
        new_at = slot_to_utc(
            chosen.date, chosen.time, practice_obj.timezone if practice_obj else None
        )

        old_at = booking.appointment_at
        booking.appointment_at = new_at
        booking.provider_name = chosen.provider
        try:
            # Same double-book guard as booking: surface a slot collision here as a
            # clean answer, not a 500 at commit.
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            booking.appointment_at = old_at  # undo the move; keep the original slot
            others = [s for s in slots
                      if not (s.date == chosen.date and s.time == chosen.time)]
            return {
                "rescheduled": False,
                "message": "I'm sorry — that time was just taken. Want one of these instead?",
                "available_slots": [
                    {"date": s.date, "time": s.time, "provider": s.provider} for s in others
                ],
            }

        practice_name = (practice_obj.name if practice_obj else None) or "our office"
        first_name = patient.first_name
        patient_opted_out = patient.sms_opt_out

        session.add(
            AuditLog(
                id=uuid.uuid4(),
                practice_id=practice_id,
                action="booking_rescheduled",
                resource_type="booking",
                resource_id=booking.id,
                audit_metadata={
                    "retell_call_id": retell_call_id,
                    "from": old_at.isoformat() if old_at else None,
                    "to": booking.appointment_at.isoformat(),
                    "patient_phone_last4": phone[-4:] if len(phone) >= 4 else "****",
                },
            )
        )
        await session.commit()
        new_time = chosen.time
        new_provider = chosen.provider

    logger.info(
        "reschedule_appointment: call=%s moved to %s %s", retell_call_id, new_date, new_time
    )
    _fire_sms(send_booking_confirmation(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=new_date,
        time=new_time,
        provider=new_provider,
        opted_out=patient_opted_out,
    ))

    return {
        "rescheduled": True,
        "appointment": {
            "date": new_date,
            "time": new_time,
            "provider": new_provider,
            "procedure": procedure,
        },
        "message": (
            f"All set — I've moved your appointment to {new_date} at {new_time} "
            f"with {new_provider}. You'll get a text confirmation."
        ),
    }


# ---------------------------------------------------------------------------
# cancel_appointment function_call handler
# ---------------------------------------------------------------------------


async def _handle_cancel_appointment(retell_call_id: str, args: dict) -> dict:
    """Cancel a patient's upcoming appointment (soonest upcoming confirmed one)."""
    phone = args.get("patient_phone", "")
    reason = args.get("reason")

    async with _app_db.async_session_factory() as session:
        practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if practice_id is None:
            return {
                "cancelled": False,
                "message": (
                    "I'm having trouble accessing the schedule — let me "
                    "have our team call you back."
                ),
            }
        await set_tenant(session, practice_id)

        patient = await _find_patient_by_phone(session, practice_id, phone)
        if patient is None:
            return {
                "cancelled": False,
                "message": "I couldn't find an appointment under that number.",
            }
        booking = await _find_upcoming_booking(session, practice_id, patient.id)
        if booking is None:
            return {
                "cancelled": False,
                "message": "I don't see an upcoming appointment to cancel.",
            }

        cancelled_date = booking.appointment_at.date().isoformat()
        cancelled_time = booking.appointment_at.strftime("%H:%M")
        booking.status = CANCELLED

        practice_name = (
            await session.execute(
                select(Practice.name).where(Practice.id == practice_id)
            )
        ).scalar_one_or_none() or "our office"
        first_name = patient.first_name
        patient_opted_out = patient.sms_opt_out

        session.add(
            AuditLog(
                id=uuid.uuid4(),
                practice_id=practice_id,
                action="booking_cancelled",
                resource_type="booking",
                resource_id=booking.id,
                audit_metadata={
                    "retell_call_id": retell_call_id,
                    "appointment_at": booking.appointment_at.isoformat(),
                    "reason": reason,
                    "patient_phone_last4": phone[-4:] if len(phone) >= 4 else "****",
                },
            )
        )

        # Backfill: a slot just freed up — notify the oldest waitlisted patient.
        # Fail-safe: a waitlist hiccup must never block the cancellation itself.
        notify_target: tuple[str | None, str | None, bool] | None = None
        try:
            notify_target = await _backfill_from_waitlist(
                session, practice_id, retell_call_id=retell_call_id
            )
        except Exception:  # noqa: BLE001 — backfill is best-effort
            logger.exception("cancel_appointment: waitlist backfill failed")

        await session.commit()

    logger.info(
        "cancel_appointment: call=%s cancelled %s %s",
        retell_call_id,
        cancelled_date,
        cancelled_time,
    )
    _fire_sms(send_cancellation_notice(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=cancelled_date,
        time=cancelled_time,
        opted_out=patient_opted_out,
    ))

    # Notify the waitlisted patient that a slot opened (after commit, fail-safe).
    if notify_target is not None:
        wl_first_name, wl_phone, wl_opted_out = notify_target
        _fire_sms(send_waitlist_opening(
            to=wl_phone,
            practice_name=practice_name,
            first_name=wl_first_name,
            date=cancelled_date,
            time=cancelled_time,
            opted_out=wl_opted_out,
        ))

    return {
        "cancelled": True,
        "message": (
            f"Done — I've cancelled your appointment on {cancelled_date} at "
            f"{cancelled_time}. Call us anytime to rebook."
        ),
    }


# ---------------------------------------------------------------------------
# join_waitlist function_call handler
# ---------------------------------------------------------------------------


async def _handle_join_waitlist(retell_call_id: str, args: dict) -> dict:
    """Add a caller to the waitlist when no suitable slot is available.

    Captures demand so a later cancellation can be backfilled. Upserts the
    patient (so we can reach them) and records their preference as free text.
    """
    first_name = args.get("patient_first_name", "Unknown")
    last_name = args.get("patient_last_name", "")
    phone = args.get("patient_phone", "")
    procedure = args.get("procedure")
    preferred_date = args.get("preferred_date")
    preferred_time_window = args.get("preferred_time_window")
    notes = args.get("notes")

    async with _app_db.async_session_factory() as session:
        result = await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )
        call = result.scalar_one_or_none()

        if call is not None:
            practice_id = call.practice_id
            call_internal_id = call.id
        else:
            resolved = await _resolve_practice(None)
            if resolved is None:
                return {
                    "added": False,
                    "message": (
                        "I'm having trouble accessing our system right now — "
                        "let me take a message and have someone call you back."
                    ),
                }
            practice_id = resolved.id
            call_internal_id = None

        await set_tenant(session, practice_id)
        patient = await _upsert_patient(session, practice_id, first_name, last_name, phone)

        entry = WaitlistEntry(
            id=uuid.uuid4(),
            practice_id=practice_id,
            patient_id=patient.id,
            call_id=call_internal_id,
            procedure_type=procedure,
            preferred_date=preferred_date,
            preferred_time_window=preferred_time_window,
            notes=notes,
            status="waiting",
        )
        session.add(entry)
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                practice_id=practice_id,
                action="waitlist_joined",
                resource_type="waitlist_entry",
                resource_id=entry.id,
                audit_metadata={
                    "retell_call_id": retell_call_id,
                    "procedure": procedure,
                    "preferred_date": preferred_date,
                    "patient_phone_last4": phone[-4:] if len(phone) >= 4 else "****",
                },
            )
        )
        await session.commit()

    logger.info("join_waitlist: call=%s patient added to waitlist", retell_call_id)
    return {
        "added": True,
        "message": (
            f"You're on our waitlist, {first_name}. If an earlier spot opens "
            f"up we'll text you right away so you can grab it."
        ),
    }


# ---------------------------------------------------------------------------
# Cancellation backfill — notify the next waitlisted patient
# ---------------------------------------------------------------------------


async def _backfill_from_waitlist(
    session,
    practice_id: uuid.UUID,
    *,
    retell_call_id: str,
) -> tuple[str | None, str | None, bool] | None:
    """Mark the oldest waiting waitlist entry as notified and return
    ``(first_name, phone, opted_out)`` for the SMS the caller should send after
    commit.

    Returns None when the waitlist is empty. Tenant must already be set.
    """
    entry = (
        await session.execute(
            select(WaitlistEntry)
            .where(
                WaitlistEntry.practice_id == practice_id,
                WaitlistEntry.status == "waiting",
            )
            .order_by(WaitlistEntry.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if entry is None:
        return None

    patient = (
        await session.execute(
            select(Patient).where(Patient.id == entry.patient_id)
        )
    ).scalar_one_or_none()

    entry.status = "notified"
    entry.notified_at = datetime.now(tz=UTC)
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            practice_id=practice_id,
            action="waitlist_notified",
            resource_type="waitlist_entry",
            resource_id=entry.id,
            audit_metadata={"retell_call_id": retell_call_id},
        )
    )
    if patient is None:
        return (None, None, False)
    return (patient.first_name, patient.phone, patient.sms_opt_out)


# ---------------------------------------------------------------------------
# lookup_patient function_call handler
# ---------------------------------------------------------------------------


async def _handle_lookup_patient(retell_call_id: str, args: dict) -> dict:
    """Recognize a returning patient by phone so the agent can greet by name.

    Returns found + first name + whether they have an upcoming appointment. The
    agent uses this to personalize ("Welcome back, Maria") and to pre-fill a
    reschedule/cancel without re-asking everything.
    """
    phone = args.get("patient_phone", "")
    not_found = {
        "found": False,
        "message": "No existing record found — proceed as a new patient.",
    }

    async with _app_db.async_session_factory() as session:
        practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if practice_id is None:
            return not_found
        await set_tenant(session, practice_id)

        patient = await _find_patient_by_phone(session, practice_id, phone)
        if patient is None:
            return not_found

        first_name = patient.first_name
        booking = await _find_upcoming_booking(session, practice_id, patient.id)
        upcoming = None
        if booking is not None:
            upcoming = {
                "date": booking.appointment_at.date().isoformat(),
                "time": booking.appointment_at.strftime("%H:%M"),
                "provider": booking.provider_name,
            }

    if upcoming:
        message = (
            f"Returning patient {first_name}; upcoming appointment on "
            f"{upcoming['date']} at {upcoming['time']}."
        )
    else:
        message = f"Returning patient {first_name}; no upcoming appointment on file."

    logger.info(
        "lookup_patient: call=%s found=True upcoming=%s", retell_call_id, bool(upcoming)
    )
    return {
        "found": True,
        "patient_first_name": first_name,
        "has_upcoming_appointment": bool(upcoming),
        "upcoming": upcoming,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Emergency-lock state: get-or-create the call row, update + read the flag.
# ---------------------------------------------------------------------------


async def _get_or_create_call(session, retell_call_id: str, agent_id: str | None) -> Call | None:
    """Return the calls row for this call, creating a minimal one if needed.

    Web/test calls don't fire call_started, so there may be no row yet — but we
    still need somewhere to PERSIST emergency_active. Returns None only when
    there's no call id or no practice to anchor the row to.
    """
    if not retell_call_id:
        return None
    # calls is RLS-protected. Resolve + bind the tenant BEFORE reading/inserting:
    # prefer the agent_id (present on most function-call payloads), else the
    # routing function (resolves the practice_id for an existing call, RLS-safe).
    practice = await _resolve_practice(agent_id)
    if practice is None:
        routed_id = await _resolve_practice_id_for_call(session, retell_call_id)
        practice = await _resolve_practice(None) if routed_id is None else None
        practice_id = routed_id or (practice.id if practice else None)
    else:
        practice_id = practice.id
    if practice_id is None:
        return None
    await set_tenant(session, practice_id)

    result = await session.execute(
        select(Call).where(Call.retell_call_id == retell_call_id)
    )
    call = result.scalar_one_or_none()
    if call is not None:
        return call

    call = Call(
        id=uuid.uuid4(),
        practice_id=practice_id,
        retell_call_id=retell_call_id,
        direction="inbound",
        from_number="unknown",
        to_number="unknown",
        started_at=datetime.now(tz=UTC),
        status="in_progress",
    )
    session.add(call)
    await session.flush()
    return call


async def _update_and_read_emergency_flag(
    fn: str, retell_call_id: str, args: dict, agent_id: str | None
) -> bool:
    """Apply the double trigger, persist the flag, and return its current value.

    Trigger (a): create_callback_request(urgent=true).
    Trigger (b): emergency keywords found in the args of ANY tool call.
    Once True the flag is sticky for the rest of the call (never cleared here).
    """
    triggered = _contains_emergency_keywords(args)
    if fn == "create_callback_request" and _is_truthy(args.get("urgent")):
        triggered = True

    async with _app_db.async_session_factory() as session:
        call = await _get_or_create_call(session, retell_call_id, agent_id)
        if call is None:
            # No row and no practice to persist against (dev/test edge). Fall
            # back to the in-args trigger for this single call so we still fail
            # safe rather than silently allowing scheduling.
            return triggered
        if triggered and not call.emergency_active:
            call.emergency_active = True
            logger.warning(
                "emergency lock ENGAGED: call=%s trigger_fn=%s", retell_call_id, fn
            )
        await session.commit()
        return bool(call.emergency_active)


async def _handle_create_callback_request(
    retell_call_id: str, args: dict, agent_id: str | None
) -> dict:
    """Persist a callback request so it surfaces on the practice dashboard.

    This is the only scheduling-adjacent action allowed during an emergency, so
    it must reliably land in the DB (previously it was only logged). Patient
    identifiers are stored encrypted via the EncryptedString column type.
    """
    first_name = args.get("patient_first_name") or args.get("first_name")
    phone = args.get("patient_phone") or args.get("phone")
    reason = args.get("reason")
    urgent = _is_truthy(args.get("urgent"))

    async with _app_db.async_session_factory() as session:
        call = await _get_or_create_call(session, retell_call_id, agent_id)
        practice_id = call.practice_id if call else None
        call_internal_id = call.id if call else None
        if practice_id is None:
            resolved = await _resolve_practice(agent_id)
            practice_id = resolved.id if resolved else None

        if practice_id is None:
            logger.warning(
                "create_callback_request: no practice found; not persisted. call=%s",
                retell_call_id,
            )
            return {
                "status": "callback_logged",
                "message": "Your callback request has been recorded; our team will reach out.",
            }

        # RLS tenant context so the insert is allowed.
        await set_tenant(session, practice_id)
        callback = CallbackRequest(
            id=uuid.uuid4(),
            practice_id=practice_id,
            call_id=call_internal_id,
            patient_first_name=first_name,
            phone=phone,
            reason=reason,
            urgent=urgent,
            status="pending",
        )
        session.add(callback)
        await session.commit()

    logger.info(
        "create_callback_request persisted: call=%s phone=%s urgent=%s",
        retell_call_id,
        (phone or "")[-4:],
        urgent,
    )
    return {
        "status": "callback_logged",
        "message": "Your callback request has been recorded; our team will reach out.",
    }


async def _handle_transfer_to_human(retell_call_id: str, args: dict) -> dict:
    """Allowed during an emergency — hands the call to a live team member.

    Resolves the practice's transfer destination (transfer_phone_number, falling
    back to its main phone_number) so Retell can bridge the call / the agent can
    read it out. Returns ``transfer_number`` (None if the practice has none set).
    """
    transfer_number = None
    async with _app_db.async_session_factory() as session:
        practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if practice_id is not None:
            row = (
                await session.execute(
                    select(
                        Practice.transfer_phone_number, Practice.phone_number
                    ).where(Practice.id == practice_id)
                )
            ).first()
            if row is not None:
                transfer_number = row[0] or row[1]

    dest_last4 = f"…{transfer_number[-4:]}" if transfer_number else "none"
    logger.info(
        "transfer_to_human: call=%s reason=%s dest=%s",
        retell_call_id,
        args.get("reason"),
        dest_last4,
    )
    return {
        "status": "transfer_initiated",
        "transfer_number": transfer_number,
        "message": "Of course — let me connect you with a team member, one moment.",
    }


# ---------------------------------------------------------------------------
# Function dispatch (shared by webhook "function_call" events AND Retell custom
# tool POSTs, which use a different payload shape — see retell_webhook below).
# ---------------------------------------------------------------------------


async def _dispatch_function(
    fn: str, retell_call_id: str, args: dict, agent_id: str | None = None
) -> dict:
    # PROGRAMMATIC EMERGENCY LOCK — runs BEFORE any tool. Persists/reads the flag
    # for this call, then physically refuses scheduling tools while active.
    emergency_active = await _update_and_read_emergency_flag(
        fn, retell_call_id, args, agent_id
    )
    if emergency_active and fn in _SCHEDULING_TOOLS:
        logger.warning(
            "emergency lock: REFUSING %s for call=%s", fn, retell_call_id
        )
        return {
            "blocked": True,
            "reason": "emergency_active",
            "message": _EMERGENCY_BLOCK_MESSAGE,
        }

    if fn == "book_appointment":
        return await _handle_book_appointment(retell_call_id, args)

    if fn == "check_availability":
        return await _handle_check_availability(retell_call_id, args)

    if fn == "lookup_patient":
        return await _handle_lookup_patient(retell_call_id, args)

    if fn == "join_waitlist":
        return await _handle_join_waitlist(retell_call_id, args)

    if fn == "reschedule_appointment":
        return await _handle_reschedule_appointment(retell_call_id, args)

    if fn == "cancel_appointment":
        return await _handle_cancel_appointment(retell_call_id, args)

    if fn == "create_callback_request":
        return await _handle_create_callback_request(retell_call_id, args, agent_id)

    if fn == "transfer_to_human":
        return await _handle_transfer_to_human(retell_call_id, args)

    logger.info("Unhandled function: %s", fn)
    return {"error": f"Unsupported function: {fn}"}


# ---------------------------------------------------------------------------
# Main webhook route
# ---------------------------------------------------------------------------


@router.post("/retell")
async def retell_webhook(request: Request) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("x-retell-signature")
    if not _verify_signature(raw_body, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad webhook signature."
        )

    payload = await request.json()
    event = payload.get("event")

    # Retell custom function tools (general_tools type=custom) POST a DIFFERENT
    # shape than lifecycle webhooks: {"call": {...}, "name": "...", "args": {...}}
    # with NO "event" field. Detect and route those to the shared dispatcher.
    if event is None and payload.get("name"):
        call_obj = payload.get("call", {}) or {}
        retell_call_id = call_obj.get("call_id") or payload.get("call_id", "")
        agent_id = call_obj.get("agent_id") or payload.get("agent_id")
        await _ensure_call_row(retell_call_id, call_obj)
        return await _dispatch_function(
            payload["name"], retell_call_id, payload.get("args", {}) or {}, agent_id
        )

    if event == "call_started":
        return await _handle_call_started(payload)

    if event == "call_ended":
        return await _handle_call_ended(payload)

    if event == "call_analyzed":
        return await _handle_call_analyzed(payload)

    if event == "function_call":
        call_obj = payload.get("call", {}) or {}
        agent_id = call_obj.get("agent_id") or payload.get("agent_id")
        await _ensure_call_row(payload.get("call_id", ""), call_obj)
        return await _dispatch_function(
            payload.get("function_name"),
            payload.get("call_id", ""),
            payload.get("args", {}) or {},
            agent_id,
        )

    logger.info("Unhandled Retell event: %s", event)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Inbound-call webhook (V1: KB → dynamic variables).
#
# Configured on the Retell PHONE NUMBER (inbound_webhook_url). Retell calls it
# BEFORE answering; whatever dynamic_variables we return are substituted into the
# agent's prompt ({{practice_name}}, {{kb_context}}, {{today}}, …) — this is what
# makes the generic agent THIS clinic's receptionist. Signature: same
# X-Retell-Signature scheme as the event webhook. On ANY failure we return an
# empty mapping rather than erroring — a broken lookup must never block the
# phone from being answered (the prompt has safe defaults).
# ---------------------------------------------------------------------------
from app.services.llm.dynamic_vars import build_dynamic_variables  # noqa: E402


async def _resolve_practice_for_inbound(agent_id: str | None,
                                        to_number: str | None) -> Practice | None:
    """Which clinic was called.

    ``to_number`` is the number the caller reached — OUR number for this clinic
    (the one they forward their line to), which is unique per practice (NUM-1).
    That makes it the reliable key. We also still match the clinic's OWN number,
    because a practice can point a line at us directly rather than forwarding.
    Falls back to agent-id routing, then the single-practice case.
    """
    if to_number:
        async with _app_db.async_session_factory() as session:
            p = (await session.execute(
                select(Practice).where(Practice.ai_phone_number == to_number)
            )).scalar_one_or_none()
            if p is not None:
                return p
            p = (await session.execute(
                select(Practice).where(Practice.phone_number == to_number)
            )).scalar_one_or_none()
            if p is not None:
                return p
    return await _resolve_practice(agent_id)


@router.post("/retell/inbound", status_code=status.HTTP_200_OK)
async def retell_inbound_webhook(request: Request) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("x-retell-signature")
    if not _verify_signature(raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Bad webhook signature.")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"call_inbound": {"dynamic_variables": {}}}

    inbound = payload.get("call_inbound") or {}
    agent_id = inbound.get("agent_id")
    to_number = inbound.get("to_number")
    try:
        practice = await _resolve_practice_for_inbound(agent_id, to_number)
        variables = build_dynamic_variables(practice) if practice else {}
    except Exception:  # noqa: BLE001 — never block call pickup on a lookup bug
        logger.exception("retell inbound webhook: variable build failed")
        variables = {}
    logger.info("retell inbound: vars=%d practice=%s",
                len(variables), "yes" if variables else "none")
    return {"call_inbound": {"dynamic_variables": variables}}
