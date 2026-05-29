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
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import app.db as _app_db
from app.config import get_settings
from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from app.models.practice import Practice
from app.services.booking import find_available_slots

logger = logging.getLogger("dentiva.webhooks.retell")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = get_settings().retell_webhook_secret
    if not secret:
        logger.warning("RETELL_WEBHOOK_SECRET empty — skipping signature check (dev only).")
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


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

    async with _app_db.async_session_factory() as session:
        result = await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )
        call = result.scalar_one_or_none()
        if call is None:
            logger.warning(
                "call_ended: no call row for retell_call_id=%s; creating one.", retell_call_id
            )
            # Resolve practice for the orphan row.
            agent_id = call_data.get("agent_id") or payload.get("agent_id")
            practice = await _resolve_practice(agent_id)
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

        # Determine outcome: booked if a booking exists for this call.
        booking_result = await session.execute(
            select(Booking.id).where(Booking.source_call_id == call.id)
        )
        booking_exists = booking_result.scalar_one_or_none() is not None
        call.outcome = "booked" if booking_exists else "info_only"
        call.status = call_status

        await session.commit()

    logger.info("call_ended persisted: retell_call_id=%s status=%s", retell_call_id, call_status)
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

    # Use first slot as the "chosen" appointment (voice agent collects patient
    # confirmation; this is the mock-PMS weekend booking).
    chosen_slot = slots[0] if slots else None

    async with _app_db.async_session_factory() as session:
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

        # Upsert patient.
        patient = await _upsert_patient(session, practice_id, first_name, last_name, phone)

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

    return {
        "result": {
            "available_slots": [
                {"date": s.date, "time": s.time, "provider": s.provider} for s in slots
            ]
        }
    }


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

    if event == "call_started":
        return await _handle_call_started(payload)

    if event == "call_ended":
        return await _handle_call_ended(payload)

    if event == "function_call":
        fn = payload.get("function_name")
        args = payload.get("args", {}) or {}
        retell_call_id = payload.get("call_id", "")

        if fn == "book_appointment":
            return await _handle_book_appointment(retell_call_id, args)

        if fn == "check_availability":
            slots = await find_available_slots(
                procedure=args.get("procedure", "cleaning"),
                preferred_date=args.get("preferred_date", ""),
                preferred_time_window=args.get("preferred_time_window"),
            )
            return {
                "result": {
                    "available_slots": [
                        {"date": s.date, "time": s.time, "provider": s.provider}
                        for s in slots
                    ]
                }
            }

        logger.info("Unhandled function_call: %s", fn)
        return {"result": {"error": f"Unsupported function: {fn}"}}

    logger.info("Unhandled Retell event: %s", event)
    return {"ok": True}
