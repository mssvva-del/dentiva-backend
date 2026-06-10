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

import hashlib
import hmac
import logging
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

import app.db as _app_db
from app.billing.metering import record_call_usage
from app.config import get_settings
from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.callback_request import CallbackRequest
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.waitlist_entry import WaitlistEntry
from app.services.booking import find_available_slots
from app.services.sms import (
    send_booking_confirmation,
    send_cancellation_notice,
    send_waitlist_opening,
)

logger = logging.getLogger("dentiva.webhooks.retell")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    settings = get_settings()
    secret = settings.retell_webhook_secret
    if not secret:
        # No secret configured → we CANNOT verify. We still allow the request so
        # the live agent keeps working, but flag it loudly. SECURITY HOLE while
        # unset: anyone who knows the URL could POST a fake book/cancel/transfer.
        # To CLOSE it: (1) set a signing secret in Retell, (2) set
        # RETELL_WEBHOOK_SECRET to match, (3) confirm our HMAC matches Retell's
        # scheme in staging BEFORE prod (mismatch would reject real webhooks).
        if settings.environment == "production":
            logger.error(
                "RETELL_WEBHOOK_SECRET NOT set — webhooks are UNVERIFIED in production."
            )
        else:
            logger.warning("RETELL_WEBHOOK_SECRET empty — skipping signature check (dev).")
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


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
    async with _app_db.async_session_factory() as session:
        if agent_id:
            result = await session.execute(
                select(Practice).where(Practice.retell_agent_id == agent_id)
            )
            practice = result.scalar_one_or_none()
            if practice:
                return practice
        # Fallback: use the first practice in the DB (single-practice weekend mode).
        result = await session.execute(select(Practice).limit(1))
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Helper — upsert / look up a patient from voice args
# ---------------------------------------------------------------------------


async def _upsert_patient(
    session,
    practice_id: uuid.UUID,
    first_name: str,
    last_name: str,
    phone: str,
) -> Patient:
    """Return existing patient by phone or create a stub for this call."""
    # Look up by encrypted phone — requires scanning; acceptable for weekend scale.
    result = await session.execute(
        select(Patient).where(Patient.practice_id == practice_id)
    )
    patients = result.scalars().all()
    for p in patients:
        try:
            if p.phone == phone:
                return p
        except Exception:
            pass

    # Create a new stub patient.
    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=f"VOICE-{uuid.uuid4().hex[:8].upper()}",
        first_name=first_name,
        last_name=last_name,
        phone=phone,
    )
    session.add(patient)
    await session.flush()
    return patient


async def _find_patient_by_phone(
    session, practice_id: uuid.UUID, phone: str
) -> Patient | None:
    """Read-only lookup of a patient by (encrypted) phone within a practice."""
    if not phone:
        return None
    result = await session.execute(
        select(Patient).where(Patient.practice_id == practice_id)
    )
    for p in result.scalars().all():
        try:
            if p.phone == phone:
                return p
        except Exception:  # noqa: BLE001 — decrypt failures on stray rows are non-fatal
            pass
    return None


async def _slot_taken(
    session, practice_id: uuid.UUID, provider_name: str | None, appointment_at: datetime
) -> bool:
    """True if a confirmed booking already holds this provider+time (double-book guard)."""
    existing = (
        await session.execute(
            select(Booking.id)
            .where(
                Booking.practice_id == practice_id,
                Booking.provider_name == provider_name,
                Booking.appointment_at == appointment_at,
                Booking.status == "confirmed",
            )
            .limit(1)
        )
    ).first()
    return existing is not None


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

    practice = await _resolve_practice(agent_id)
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
    resolved_practice = await _resolve_practice(agent_id)

    async with _app_db.async_session_factory() as session:
        if resolved_practice is not None:
            await set_tenant(session, resolved_practice.id)
        result = await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
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

        # Determine outcome: booked if a booking exists for this call.
        booking_result = await session.execute(
            select(Booking.id).where(Booking.source_call_id == call.id)
        )
        booking_exists = booking_result.scalar_one_or_none() is not None
        call.outcome = "booked" if booking_exists else "info_only"
        call.status = call_status

        # Billing metering (Phase D): a completed call adds its minutes to the
        # practice's current-period usage. Only count answered calls (a 'missed'
        # call consumed no agent minutes). Best-effort — never fail the webhook
        # over metering, but log loudly so under-billing is visible.
        if call_status == "completed":
            try:
                await record_call_usage(session, call.practice_id, duration_seconds)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "metering failed for call %s (practice %s)",
                    retell_call_id, call.practice_id,
                )

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

    # Extract structured fields
    call_intent = analysis.get("intent")  # enum string
    patient_sentiment = analysis.get("patient_sentiment")  # enum string
    escalation_needed = analysis.get("escalation_needed")  # bool
    hipaa_compliant = analysis.get("hipaa_compliant")  # bool

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


async def _handle_book_appointment(retell_call_id: str, args: dict) -> dict:
    slots = await find_available_slots(
        procedure=args.get("procedure", "cleaning"),
        preferred_date=args.get("preferred_date", ""),
        preferred_time_window=args.get("preferred_time_window"),
    )

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
                # No practice in DB — return slots without persistence (dev/test edge case).
                logger.warning(
                    "book_appointment: no practice found; returning slots without persistence."
                )
                return {
                    "result": {
                        "available_slots": [
                            {"date": s.date, "time": s.time, "provider": s.provider}
                            for s in slots
                        ]
                    }
                }
            practice_id = resolved_practice.id

        # Set tenant context so RLS allows patient inserts.
        await set_tenant(session, practice_id)

        # Practice name for the confirmation SMS (sent after commit).
        if resolved_practice is not None:
            practice_name = resolved_practice.name
        else:
            practice_name = (
                await session.execute(
                    select(Practice.name).where(Practice.id == practice_id)
                )
            ).scalar_one_or_none() or "our office"

        # Upsert patient.
        patient = await _upsert_patient(session, practice_id, first_name, last_name, phone)
        patient_opted_out = patient.sms_opt_out

        # Double-book guard: pick the first offered slot not already taken.
        for s in slots:
            cand = datetime.fromisoformat(f"{preferred_date}T{s.time}:00+00:00")
            if not await _slot_taken(session, practice_id, s.provider, cand):
                chosen_slot = s
                break
        if slots and chosen_slot is None:
            # Every offered time was just booked by someone else.
            return {
                "booked": False,
                "message": (
                    "I'm sorry — those times were just taken. "
                    "Want me to check another day?"
                ),
                "available_slots": [],
            }

        # Create booking row.
        slot_time = chosen_slot.time if chosen_slot else "09:00"
        appointment_at_str = f"{preferred_date}T{slot_time}:00+00:00"
        appointment_at = datetime.fromisoformat(appointment_at_str)
        booking = Booking(
            id=uuid.uuid4(),
            practice_id=practice_id,
            patient_id=patient.id,
            source_call_id=call_internal_id,
            appointment_at=appointment_at,
            duration_minutes=60,
            procedure_type=procedure,
            provider_name=chosen_slot.provider if chosen_slot else "Dr. Smith",
            status="confirmed",
            source="ai_call",
        )
        session.add(booking)
        await session.flush()

        # Update call outcome if we have the call row.
        if call:
            call.outcome = "booked"

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
    booked_time = chosen_slot.time if chosen_slot else "09:00"
    booked_provider = chosen_slot.provider if chosen_slot else "Dr. Smith"
    sms_result = await send_booking_confirmation(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=preferred_date,
        time=booked_time,
        provider=booked_provider,
        opted_out=patient_opted_out,
    )
    logger.info("book_appointment: sms %s", sms_result)

    return {
        "booked": True,
        "appointment": {
            "date": preferred_date,
            "time": chosen_slot.time if chosen_slot else "09:00",
            "provider": chosen_slot.provider if chosen_slot else "Dr. Smith",
            "procedure": procedure,
        },
        "message": (
            f"Appointment confirmed for {first_name} on {preferred_date} "
            f"at {chosen_slot.time if chosen_slot else '09:00'} "
            f"with {chosen_slot.provider if chosen_slot else 'Dr. Smith'}."
        ),
        "available_slots": [
            {"date": s.date, "time": s.time, "provider": s.provider} for s in slots
        ],
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
        slots = await find_available_slots(
            procedure=procedure,
            preferred_date=new_date,
            preferred_time_window=new_window,
        )
        if not slots:
            return {
                "rescheduled": False,
                "message": (
                    f"I don't have any openings on {new_date}. "
                    "Want me to check another day?"
                ),
                "available_slots": [],
            }

        # Double-book guard: pick the first offered slot not already taken.
        chosen = None
        new_at = None
        for s in slots:
            cand = datetime.fromisoformat(f"{new_date}T{s.time}:00+00:00")
            if not await _slot_taken(session, practice_id, s.provider, cand):
                chosen = s
                new_at = cand
                break
        if chosen is None:
            return {
                "rescheduled": False,
                "message": (
                    f"Those {new_date} times were just taken. "
                    "Want me to check another day?"
                ),
                "available_slots": [],
            }

        old_at = booking.appointment_at
        booking.appointment_at = new_at
        booking.provider_name = chosen.provider

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
    sms_result = await send_booking_confirmation(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=new_date,
        time=new_time,
        provider=new_provider,
        opted_out=patient_opted_out,
    )
    logger.info("reschedule_appointment: sms %s", sms_result)

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
        booking.status = "cancelled"

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
    sms_result = await send_cancellation_notice(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=cancelled_date,
        time=cancelled_time,
        opted_out=patient_opted_out,
    )
    logger.info("cancel_appointment: sms %s", sms_result)

    # Notify the waitlisted patient that a slot opened (after commit, fail-safe).
    if notify_target is not None:
        wl_first_name, wl_phone, wl_opted_out = notify_target
        wl_sms = await send_waitlist_opening(
            to=wl_phone,
            practice_name=practice_name,
            first_name=wl_first_name,
            date=cancelled_date,
            time=cancelled_time,
            opted_out=wl_opted_out,
        )
        logger.info("cancel_appointment: waitlist backfill sms %s", wl_sms)

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
        slots = await find_available_slots(
            procedure=args.get("procedure", "cleaning"),
            preferred_date=args.get("preferred_date", ""),
            preferred_time_window=args.get("preferred_time_window"),
        )
        return {
            "available_slots": [
                {"date": s.date, "time": s.time, "provider": s.provider} for s in slots
            ]
        }

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
        return await _dispatch_function(
            payload.get("function_name"),
            payload.get("call_id", ""),
            payload.get("args", {}) or {},
            agent_id,
        )

    logger.info("Unhandled Retell event: %s", event)
    return {"ok": True}
