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
from app.services.availability import (
    VOICE_PATIENT_PREFIX,
    compute_native_slots,
    compute_pms_slots,
    pms_is_connected,
    slot_to_utc,
    visit_minutes,
)
from app.services.call_outcome import BOOKED, classify_outcome
from app.services.call_sync import slim_transcript
from app.services.sms import (
    page_clinic_new_booking,
    page_clinic_urgent_callback,
    send_booking_confirmation,
    send_cancellation_notice,
    send_waitlist_opening,
)
from app.utils.crypto import normalize_phone, phone_hmac

logger = logging.getLogger("dentiva.webhooks.retell")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Fire confirmation/cancellation SMS WITHOUT blocking the live agent's tool
# response — Twilio can take up to 15s, which is dead air on the call. The send
# functions are self-contained (no request session) and fail-safe, so a detached
# task is safe. Keep a reference so the task isn't garbage-collected mid-flight.
_bg_sms_tasks: set = set()


def _fire_sms(coro, *, critical: str = "") -> None:
    """Send without making the caller wait. Optionally, notice when it did not.

    Nothing looked at the result. send_sms returns {"skipped": ...} when SMS is
    switched off or Twilio is not configured, and those two branches log at INFO
    and alert nobody — so the whole mechanism could be inert and every call would
    still sound perfect.

    That is survivable for a confirmation text. It is not survivable for the page
    that tells a clinic someone is bleeding, because the agent has already said
    the team has been notified. ``critical`` names the promise so a page that
    never left raises an alert with the right words on it.
    """
    async def _watched() -> None:
        result = await coro
        if critical and isinstance(result, dict) and not result.get("sid"):
            reason = result.get("skipped") or result.get("error") or "unknown"
            record_alert(f"page_not_delivered_{critical}", f"reason={reason}")
            logger.error(
                "PAGE NOT DELIVERED (%s): %s — the caller was told the clinic "
                "had been notified", critical, reason,
            )

    task = asyncio.create_task(_watched() if critical else coro)
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
# The prompt promises Spanish and switches to it on the caller's first words, so
# an English-only safety net protects only half the callers it exists for. A
# patient describing an airway in Spanish must trip the same deterministic lock.
_EMERGENCY_PATTERNS = (
    r"sangr",              # sangrado, sangrando, sangra
    # "hinch" catches the whole family — hinchazón, hinchado, and the VERB a
    # patient actually uses: "se me hincha la cara", "se me está hinchando".
    # Matching only the noun missed the most natural way to say it.
    r"hinch",
    r"inflamaci",          # inflamación
    r"dolor\s+(muy\s+)?(fuerte|intenso|severo|insoportable)",
    r"respir",             # respirar, respiración, respiro
    r"traga",              # tragar
    r"emergencia",
    r"urgen",              # urgente, urgencia
    r"bleed",
    r"blood",              # "a lot of blood" — the noun was missing entirely
    r"swell",
    r"swollen",
    r"severe\s+pain",
    r"uncontroll",
    r"breath",
    r"knocked[\s-]?out",
    r"se\s+me\s+cay[oó]\s+un\s+diente",   # tooth knocked out
    r"golpe\s+en\s+(la\s+)?(cara|boca|diente)",
    r"emergency",
    r"urgent",
)
_EMERGENCY_RE = re.compile("|".join(_EMERGENCY_PATTERNS), re.IGNORECASE)

# A SECOND, NARROWER tier inside the emergency lock: signs that belong in an
# emergency room, not in our callback queue. The wide _EMERGENCY_PATTERNS above
# cover "urgent" dental pain — those are ours to call back about. These are not:
# an airway or an uncontrolled bleed cannot wait for the office to open, and
# after hours a callback promise is actively dangerous ("the team will call you
# back" at 11pm = wait until morning).
_MEDICAL_ER_PATTERNS = (
    # Spanish first — same tier-1 signs, same consequence.
    r"no\s+puedo\s+(respirar|tragar)",
    r"(dificultad|problemas?)\s+(para|al)\s+(respirar|tragar)",
    r"me\s+(cuesta|falta)\s+(respirar|el\s+aire)",
    r"no\s+para\s+de\s+sangrar",
    r"(mucha\s+)?sangre\b[^.!?;]{0,30}\bno\s+para",   # "sangre … y no para"
    r"(a\s+lot\s+of\s+)?blood[^.!?;]{0,30}(wo|does)?n'?t\s+stop",
    r"sangr\w*\s+que\s+no\s+(para|se\s+detiene)",
    # Either order, because Spanish puts the verb first as readily as the
    # adjective: "se me hincha la cara" and "cara hinchada" are the same
    # emergency. Matching only the adjective form missed the sentence a patient
    # actually says out loud.
    r"hinch\w*[^.!?;]{0,15}(cara|garganta|cuello|lengua|labio|ojo)",
    r"(cara|garganta|cuello|lengua|labio|ojo)[^.!?;]{0,15}hinch",
    r"se\s+desmay",         # se desmayó
    r"perdi[oó]\s+el\s+conocimiento",
    r"inconsciente",
    r"(can'?t|cannot|hard\s+to|trouble|difficulty)\s+(breath|swallow)",
    r"(breath|swallow)\w*\s+(is\s+)?(hard|difficult)",
    r"airway",
    r"choking",
    r"(uncontroll\w*|heavy|profuse)\s+bleed",
    r"bleed\w*\s+(that\s+)?(won'?t|will\s+not|doesn'?t)\s+stop",
    r"won'?t\s+stop\s+bleed",
    r"(face|facial|throat|neck|tongue|eye)\s+(is\s+)?swell",
    r"swollen\s+(face|throat|neck|tongue|eye)",
    r"swelling\s+(in|of)\s+(my\s+)?(face|throat|neck|tongue)",
    r"passed\s+out",
    r"unconscious",
    r"lost\s+consciousness",
    r"knocked\s+unconscious",
)
_MEDICAL_ER_RE = re.compile("|".join(_MEDICAL_ER_PATTERNS), re.IGNORECASE)

# Spoken to the caller INSTEAD of a callback promise. Independent of the hour and
# of what the model decided — the office being open does not make an airway ours.
_ER_REFERRAL_MESSAGE = (
    "Tell the caller, calmly and immediately: this needs emergency care now — "
    "hang up and call 911, or go to the nearest emergency room. Do NOT offer an "
    "appointment, a callback, or a hold as the next step. Say the team has been "
    "notified and will follow up afterwards."
)


def _needs_emergency_room(args: dict) -> bool:
    """Life-threatening signs in ANY tool's arguments — deterministic, no LLM."""
    return _matched_and_not_negated(_MEDICAL_ER_RE, _args_to_text(args))


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


# Arguments whose values come from a FIXED LIST, not from the caller's mouth.
# Scanning them is how "procedure=emergency" — a visit type we advertise and let
# the agent book — set the emergency flag on the caller's very first
# check_availability, refused that call, and then stayed set for the rest of the
# conversation, so nothing could be booked at all. The symptom scan belongs on
# free text only.
_ENUM_ARG_KEYS = frozenset({
    "procedure", "preferred_time_window", "new_time_window", "language",
    "preferred_date", "new_date", "preferred_time", "patient_phone", "phone",
})

# "Any swelling, trouble swallowing, bleeding that won't stop?" is a question the
# prompt tells the agent to ask. The answer is usually NO, and a plain substring
# scan locked the call on the patient saying so.
# The gap between the negation and the symptom must not cross a contrast word:
# "no fever BUT my face is swelling" is not a denial of the swelling, and reading
# it as one is the failure direction that gets someone hurt.
_NEGATION_RE = re.compile(
    r"\b(no|not|none|without|denies|denied|isn'?t|aren'?t|doesn'?t|don'?t|"
    r"nothing|never|negative\s+for|"
    # Spanish: "sin hinchazón", "ninguna fiebre", "tampoco sangra"
    r"sin|ninguna?|nada\s+de|tampoco)\b"
    r"(?:(?!\b(?:but|however|though|although|yet|except|pero|aunque|sin\s+embargo)\b)[^.!?;]){0,40}$",
    re.IGNORECASE,
)


def _args_to_text(args: dict) -> str:
    """Flatten the FREE-TEXT arg values into one string for keyword scanning."""
    parts: list[str] = []
    for key, value in args.items():
        if key in _ENUM_ARG_KEYS:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value is not None and not isinstance(value, (dict, bool)):
            parts.append(str(value))
    return " ".join(parts)


# A negation that is part of the symptom, not a denial of it. Spanish is where
# this bites hardest: "no puedo respirar" is I CANNOT BREATHE, and reading the
# leading "no" as a denial silences the most urgent sentence a caller can say.
# English has the same shape in "won't stop bleeding".
_SYMPTOM_NEGATIONS = (
    r"no\s+pued\w*",           # no puedo / no puede respirar, tragar
    r"no\s+para\b",            # "no para de sangrar" AND "…y no para"
    r"no\s+se\s+detiene",
    r"no\s+me\s+deja",         # no me deja respirar
    r"no\s+le\s+llega",        # no le llega el aire
    r"(wo|does)?n'?t\s+stop",
    r"can'?t\s+(stop|breath|swallow)",
    r"cannot\s+(stop|breath|swallow)",
)
_SYMPTOM_NEGATION_RE = re.compile(
    "(?:" + "|".join(_SYMPTOM_NEGATIONS) + r")[^.!?;]{0,20}$", re.IGNORECASE
)


def _matched_and_not_negated(pattern: re.Pattern, text: str) -> bool:
    """True when the pattern matches somewhere it isn't being DENIED.

    Deliberately asymmetric: a negation only suppresses the words it precedes
    within the same clause, so "no swelling but I can't breathe" still fires on
    the second half. Under-triggering here is the dangerous direction.
    """
    for match in pattern.finditer(text):
        before = text[: match.start()]
        if _SYMPTOM_NEGATION_RE.search(before):
            return True  # the "no" belongs to the symptom
        if not _NEGATION_RE.search(before):
            return True
    return False


def _contains_emergency_keywords(args: dict) -> bool:
    text = _args_to_text(args)
    # Anything severe enough for an emergency room must also stop scheduling. The
    # two scans were independent, so a sign we route to 911 ("se desmayó") could
    # still leave booking allowed if the wider keyword list happened to miss it.
    return _matched_and_not_negated(_MEDICAL_ER_RE, text) or _matched_and_not_negated(
        _EMERGENCY_RE, text
    )


# ---------------------------------------------------------------------------
# Helper — look up practice by retell_agent_id or fall back to the first seed
# ---------------------------------------------------------------------------


async def _resolve_practice(
    agent_id: str | None, to_number: str | None = None
) -> Practice | None:
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
        # The number that was dialled. Every clinic gets its own Dentovox number
        # at provisioning (NUM-1) and it is written to practices.ai_phone_number,
        # so this is the one key that is actually populated. We also match the
        # clinic's own number, because a practice can point its line at us
        # directly instead of forwarding.
        for column in (Practice.ai_phone_number, Practice.phone_number):
            if not to_number or to_number == "unknown":
                break
            practice = (await session.execute(
                select(Practice).where(column == to_number)
            )).scalars().first()
            if practice is not None:
                return practice

        # Agent id, kept for web calls and for a clinic given its own agent later.
        # NOTHING writes practices.retell_agent_id today, so this matches nothing
        # in practice — it is a hook, not a route. first() rather than
        # scalar_one_or_none() on purpose: every clinic currently shares one
        # Retell agent, so anyone "fixing" routing by copying that id onto each
        # practice row would otherwise turn call_started into a 500 and a retry
        # storm. Ambiguous is logged and refused below, never raised.
        if agent_id:
            matches = (await session.execute(
                select(Practice).where(Practice.retell_agent_id == agent_id).limit(2)
            )).scalars().all()
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                logger.critical(
                    "resolve_practice: agent_id=%r is on more than one practice — "
                    "refusing to route rather than guess which clinic called.",
                    agent_id,
                )
                return None

        # No match: only safe to guess in the unambiguous single-practice case.
        rows = (await session.execute(select(Practice).limit(2))).scalars().all()
        if len(rows) == 1:
            return rows[0]
        if not rows:
            return None
        logger.critical(
            "resolve_practice: REFUSING to route — to_number=%r and agent_id=%r "
            "matched no practice, and %d practices exist. The number is the key: "
            "check that this clinic was provisioned (practices.ai_phone_number). "
            "Returning None rather than guessing, which would be a cross-tenant "
            "data leak.",
            to_number, agent_id, len(rows),
        )
        return None


# ---------------------------------------------------------------------------
# Helper — upsert / look up a patient from voice args
# ---------------------------------------------------------------------------


AMBIGUOUS = "ambiguous"
ASK_WHICH_PATIENT = (
    "There are a couple of people at this number in our records. Could you tell "
    "me your date of birth so I pull up the right one?"
)


async def _upsert_patient(
    session,
    practice_id: uuid.UUID,
    first_name: str,
    last_name: str,
    phone: str,
    language: str | None = None,
) -> Patient | str:
    """Return existing patient by phone or create a stub for this call.

    ``language`` is the language this call was conducted in. For a NEW patient we
    store it as preferred_language so downstream SMS + reactivation speak their
    language. We do NOT overwrite an existing patient's stored preference (a
    relative may call in a different language)."""
    # Indexed lookup by the deterministic phone hash — no full-table scan / decrypt.
    existing = await _find_patient_by_phone(
        session, practice_id, phone, first_name=first_name
    )
    if existing is AMBIGUOUS:
        # Two people on this number already answer to this name — a junior and a
        # senior, most often. Attaching to either is a coin flip on whose chart
        # the appointment lands in, so the caller answers it, not us.
        return AMBIGUOUS
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
    session,
    practice_id: uuid.UUID,
    phone: str,
    *,
    first_name: str | None = None,
    dob: str | None = None,
) -> Patient | str | None:
    """The patient this number belongs to, ``AMBIGUOUS``, or None.

    A dental practice's records are full of shared numbers: one household line,
    parents and children, a couple. Picking the OLDEST match, as this used to,
    means the family's first-registered patient absorbs everyone else's calls —
    the husband rings from the family phone and reaches his wife's chart, passes
    the caller-ID check on it, and can move her appointment believing it is his.

    So we disambiguate by whatever the caller has given us. A first name is what
    booking always has; a date of birth is what the identity challenge asks for.
    When several people share the number and nothing separates them, the answer
    is AMBIGUOUS — the agent must ask rather than the code guess. Guessing here
    is not a wrong record, it is the wrong person's medical appointment.
    """
    h = phone_hmac(phone)
    if not h:
        return None
    rows = (
        await session.execute(
            select(Patient)
            .where(Patient.practice_id == practice_id, Patient.phone_hmac == h)
            .order_by(Patient.created_at.asc())
        )
    ).scalars().all()
    if not rows:
        return None

    # Narrow by whatever the caller gave us. The date of birth is what the
    # identity challenge asks for and is the stronger of the two; the first name
    # is what booking always has.
    if dob:
        rows = [p for p in rows if _dob_matches(p.date_of_birth, dob)]
    elif first_name:
        wanted = first_name.strip().casefold()
        rows = [p for p in rows if (p.first_name or "").strip().casefold() == wanted]

    if len(rows) == 1:
        return rows[0]
    if not rows:
        # Nobody at this number answers to that name or that birthday. For a
        # booking this is the ordinary case — a new family member joining the
        # household line — so it is "not found", not "which one of you?".
        #
        # It also means a returning patient who gives a nickname ("Mike" for a
        # record reading "Michael") gets a second stub rather than the first one.
        # That is the side to err on: a duplicate record is something the front
        # desk merges, whereas attaching to the wrong person is a stranger's
        # appointment in someone's chart.
        return None

    logger.warning(
        "phone lookup: %d patients share this number in practice %s — asking, not guessing",
        len(rows), practice_id,
    )
    return AMBIGUOUS


async def _find_upcoming_booking(
    session, practice_id: uuid.UUID, patient_id: uuid.UUID,
    only_ids: set[uuid.UUID] | None = None,
) -> Booking | None:
    """The soonest upcoming confirmed booking for a patient, if any.

    ``only_ids`` narrows the search to specific bookings. A caller who has proved
    nothing beyond creating an appointment on this call may act on that
    appointment — not on the earlier one the same patient record already had,
    which is the soonest and therefore what an unrestricted search would return.
    """
    now = datetime.now(tz=UTC)
    scope = (
        [Booking.id.in_(only_ids)] if only_ids else []
    )
    result = await session.execute(
        select(Booking)
        .where(
            Booking.practice_id == practice_id,
            Booking.patient_id == patient_id,
            Booking.status == "confirmed",
            Booking.appointment_at >= now,
            *scope,
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
    """Which clinic this call belongs to.

    ``metadata.practice_id`` wins when present — the web-call demo carries it,
    because every web call shares one agent and the agent id cannot tell them
    apart. Otherwise the number that was dialled decides.
    """
    to_number = call_data.get("to_number")
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
    return await _resolve_practice(agent_id, to_number)


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

    # Retell sends BOTH a flat "transcript" string and a "transcript_object" list
    # carrying real agent/user roles. Take the roles when they are there — every
    # reader downstream (the broken-promise scan above all) works by role, and a
    # transcript stored with role "raw" is invisible to all of them.
    transcript_jsonb = slim_transcript(call_data) or slim_transcript(payload)

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




async def _sync_booking_change_to_pms(booking_id, practice_id, action: str) -> None:
    """Carry a cancellation or a move into the clinic's own calendar. Never raises.

    Both used to stop at our database. A cancellation that only happens here is
    the loss this product is sold to prevent, inverted: the patient is gone, the
    front desk still sees them booked, and the hour stays blocked against
    everyone else. A move is worse — the clinic holds the old time and knows
    nothing of the new one, so one call produces two wrong entries.

    Runs AFTER our side is committed, so the two can only disagree in the
    direction the front desk can see and fix.
    """
    from app.services.reactivation.writeback import cancel_in_pms, move_in_pms

    try:
        async with _app_db.async_session_factory() as session:
            await set_tenant(session, practice_id)
            booking = (await session.execute(
                select(Booking).where(Booking.id == booking_id)
            )).scalar_one_or_none()
            if booking is None:
                return
            fn = cancel_in_pms if action == "cancel" else move_in_pms
            status = await fn(session, practice_id, booking)
    except Exception:  # noqa: BLE001 — a PMS problem is never worth a 500 mid-call
        logger.exception("PMS %s crashed for booking %s", action, booking_id)
        record_alert(f"pms_{action}_crashed", f"booking={booking_id}")
        return

    # no_pms: this clinic has no calendar but ours. no_pms_record: the booking
    # never reached the PMS, so there is nothing there to change. Neither is a
    # failure, and alerting on them would bury the two that are.
    if status not in ("cancelled", "moved", "no_pms", "no_pms_record"):
        record_alert(f"pms_{action}_{status}", f"booking={booking_id}")
    logger.info("PMS %s %s for booking %s", action, status, booking_id)


async def _write_booking_to_pms(
    practice_id, booking_id, patient_pms_id: str, provider_id: str, operatory_id
) -> None:
    """Write a confirmed booking into the clinic's PMS. Never raises.

    The booking already exists on our side and the caller has already been told
    it's confirmed, so a PMS failure must not undo any of that — it degrades to
    an alert the clinic can act on. write_back_booking re-checks the slot is
    still free in the PMS first and reports 'conflict' rather than double-booking
    a real patient.
    """
    from app.services.reactivation.writeback import write_back_booking

    try:
        async with _app_db.async_session_factory() as session:
            await set_tenant(session, practice_id)
            booking = (await session.execute(
                select(Booking).where(Booking.id == booking_id)
            )).scalar_one_or_none()
            if booking is None:
                return
            status = await write_back_booking(
                session, practice_id, booking,
                patient_pms_id=patient_pms_id,
                provider_id=provider_id,
                operatory_id=operatory_id,
            )
    except Exception:  # noqa: BLE001 — a PMS problem is never worth a 500 mid-call
        logger.exception("PMS write-back crashed for booking %s", booking_id)
        record_alert("pms_write_crashed", f"booking={booking_id}")
        return

    if status not in ("written", "no_pms"):
        # 'conflict' is the one that needs a human NOW: the clinic's calendar took
        # that time while we were on the phone, so the patient is holding a slot
        # the practice no longer has.
        #
        # 'no_pms' is not a failure. A clinic with no practice-management system
        # connected is a clinic whose whole calendar is our book, and alerting on
        # every booking there would bury the statuses that mean something.
        record_alert(f"pms_write_{status}", f"booking={booking_id}")
    logger.info("PMS write-back %s for booking %s", status, booking_id)


async def _open_slots(session, practice, *, procedure, preferred_date, preferred_window):
    """Times we can actually offer — from the clinic's PMS when it has one.

    Our own book knows nothing about walk-ins, the front desk booking directly, or
    a second staff member's phone call, so on a connected practice the PMS is the
    only honest answer. When the PMS can't answer we fall back to our own book and
    alert: offering a slot we're less sure about beats leaving a caller with none.
    """
    if pms_is_connected(practice):
        pms_slots = await compute_pms_slots(
            practice,
            preferred_date=preferred_date,
            preferred_window=preferred_window,
            procedure=procedure,
        )
        if pms_slots is not None:
            return pms_slots
        record_alert("pms_slots_unavailable", f"practice={practice.id}")
    return await compute_native_slots(
        session, practice,
        procedure=procedure,
        preferred_date=preferred_date,
        preferred_window=preferred_window,
    )


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
        slots = await _open_slots(
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
        # Read while the session is open: after commit the instance is expired, and
        # touching it below (the SMS fires outside the block) would lazy-load.
        clinic_alert_line = (
            (practice_obj.transfer_phone_number or practice_obj.phone_number)
            if practice_obj is not None and practice_obj.booking_alerts_enabled
            else None
        )

        # Language this call was conducted in — the agent passes it, falling back
        # to Retell's mid-call detection on the call row. Drives preferred_language
        # for the new patient and the confirmation SMS language.
        call_language = args.get("language") or (call.language_detected if call else None)

        # Upsert patient (stores preferred_language on a new patient).
        patient = await _upsert_patient(
            session, practice_id, first_name, last_name, phone, language=call_language
        )
        if patient is AMBIGUOUS:
            return {"booked": False, "message": ASK_WHICH_PATIENT}
        patient_opted_out = patient.sms_opt_out

        # REAL openings from the clinic's own hours minus booked slots (same source
        # check_availability offered). Honor an exact time if the agent passed one,
        # else take the first free slot in the requested window.
        wanted_time = (args.get("preferred_time") or "").strip()
        slots = []
        if practice_obj is not None:
            slots = await _open_slots(
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
            # The length THIS clinic books this procedure at — the same number
            # the slot was sized with. Storing a flat 60 under a 90-minute slot
            # leaves half an hour that availability believes is free, and offers
            # it to the next caller on top of a patient already in the chair.
            duration_minutes=visit_minutes(practice_obj, procedure) if practice_obj else 60,
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

        # Gather what the PMS write needs before the session closes and these
        # instances expire. None = nothing to write (no PMS, or this patient
        # doesn't exist in it yet).
        pms_write_target = None
        if practice_obj is not None and pms_is_connected(practice_obj):
            patient_pms_id = patient.pms_external_id or ""
            if not patient_pms_id or patient_pms_id.startswith(VOICE_PATIENT_PREFIX):
                # We met this person on the phone; the clinic's system has never
                # heard of them. Writing an appointment against an unknown patient
                # is not possible, so the front desk has to add them by hand — and
                # must be told, or the slot silently exists only here.
                record_alert(
                    "pms_patient_not_in_pms",
                    f"practice={practice_id} booking={booking.id}",
                )
            elif not chosen_slot.prov_num:
                record_alert(
                    "pms_slot_without_provider",
                    f"practice={practice_id} booking={booking.id}",
                )
            else:
                pms_write_target = (
                    practice_id, booking.id, patient_pms_id,
                    chosen_slot.prov_num, chosen_slot.op_num,
                )

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

    # Put it in the clinic's own calendar when they have one. Their front desk
    # works from the PMS, not from us, so a booking that lives only here is the
    # double-booking we keep warning about.
    if pms_write_target is not None:
        await _write_booking_to_pms(*pms_write_target)

    # And tell the CLINIC — an AI booking they never see is a double-booking
    # waiting to happen while there's no PMS sync. Name + time only.
    if clinic_alert_line:
        # Named too: while no PMS sync exists this text is the only thing between
        # our booking and the front desk writing someone else into the same
        # chair. Quieter than the urgent page; a silent failure still costs a
        # real slot and an argument with a patient standing at the desk.
        _fire_sms(critical="new_booking", coro=page_clinic_new_booking(
            to=clinic_alert_line,
            practice_name=practice_name,
            first_name=first_name,
            date=booked_date,
            time=booked_time,
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

        patient = await _find_patient_by_phone(
            session, practice_id, phone, dob=args.get("patient_dob")
        )
        if patient is AMBIGUOUS:
            return {"rescheduled": False, "message": ASK_WHICH_PATIENT}
        if patient is None:
            return {
                "rescheduled": False,
                "message": (
                    "I couldn't find an appointment under that number. "
                    "Would you like to book a new one?"
                ),
            }
        call_row = (await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )).scalar_one_or_none()
        allowed = await _caller_is_verified(session, call_row, patient, args)
        if allowed is None:
            record_alert("identity_challenge", f"call={retell_call_id} tool=reschedule")
            return {"rescheduled": False, "verify_identity": True,
                    "message": _IDENTITY_CHALLENGE}
        booking = await _find_upcoming_booking(
            session, practice_id, patient.id, only_ids=allowed
        )
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
        patient_language = patient.preferred_language

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
        # The DATE WE STORED, not the one that was asked for. compute_native_slots
        # treats preferred_date as a starting point and scans forward two weeks,
        # so when the requested day is full, chosen is a later one. Telling the
        # patient "Friday" while the booking says Monday sends them to a clinic
        # that is not expecting them, and the clinic blames the AI. book_
        # appointment was fixed for exactly this; reschedule was not.
        new_date = chosen.date
        new_time = chosen.time
        new_provider = chosen.provider
        booking_id_for_pms = booking.id

    logger.info(
        "reschedule_appointment: call=%s moved to %s %s", retell_call_id, new_date, new_time
    )
    await _sync_booking_change_to_pms(booking_id_for_pms, practice_id, "move")
    _fire_sms(send_booking_confirmation(
        to=phone,
        practice_name=practice_name,
        first_name=first_name,
        date=new_date,
        time=new_time,
        provider=new_provider,
        opted_out=patient_opted_out,
        # book_appointment passes this and reschedule did not, so a patient whose
        # record says Spanish got the move in English — from the same agent that
        # had just spoken Spanish to them.
        language=patient_language,
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

        patient = await _find_patient_by_phone(
            session, practice_id, phone, dob=args.get("patient_dob")
        )
        if patient is AMBIGUOUS:
            return {"cancelled": False, "message": ASK_WHICH_PATIENT}
        if patient is None:
            return {
                "cancelled": False,
                "message": "I couldn't find an appointment under that number.",
            }
        call_row = (await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )).scalar_one_or_none()
        allowed = await _caller_is_verified(session, call_row, patient, args)
        if allowed is None:
            record_alert("identity_challenge", f"call={retell_call_id} tool=cancel")
            return {"cancelled": False, "verify_identity": True,
                    "message": _IDENTITY_CHALLENGE}
        booking = await _find_upcoming_booking(
            session, practice_id, patient.id, only_ids=allowed
        )
        if booking is None:
            return {
                "cancelled": False,
                "message": "I don't see an upcoming appointment to cancel.",
            }

        cancelled_date = booking.appointment_at.date().isoformat()
        cancelled_time = booking.appointment_at.strftime("%H:%M")
        booking.status = CANCELLED
        # Captured before the session closes: the PMS call runs on its own
        # session afterwards, and touching an expired instance there would fail
        # in a way that reads as a PMS problem.
        booking_id_for_pms = booking.id

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
                    # NOT the reason itself. Audit metadata is plain JSONB, read
                    # by internal screens and included in any database dump — and
                    # a cancellation reason is whatever the patient said, which
                    # is routinely clinical ("the swelling got worse").
                    "reason_given": bool(reason),
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
    await _sync_booking_change_to_pms(booking_id_for_pms, practice_id, "cancel")
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
        if patient is AMBIGUOUS:
            return {"added": False, "message": ASK_WHICH_PATIENT}

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



# ---------------------------------------------------------------------------
# WHO IS ASKING — identity check for the tools that read or change a record.
#
# lookup_patient / reschedule_appointment / cancel_appointment take a phone
# number straight from the model's tool call. Nothing compared it to the line the
# caller is actually on, and the prompt's "verify name and date of birth" had
# nowhere to land: the tool schemas had no DOB field at all. So "can you check
# my ex's appointment, her number is …" returned her name and visit time, and
# "reschedule it" moved it.
#
# A human receptionist asks for something only the patient knows. This does the
# same, in code:
#   * calling from the number on the record → that's the strong signal, proceed;
#   * calling from anywhere else → the date of birth must match what we hold.
# A record with no DOB on file can't be verified that way, so it falls back to
# requiring the matching line — a patient we know nothing about is the one to be
# most careful with, not least.
# ---------------------------------------------------------------------------

_IDENTITY_CHALLENGE = (
    "Ask the caller for the date of birth on the appointment, then call this "
    "again with patient_dob. Say you just need to confirm you're speaking with "
    "the right person before you look anything up. Do NOT read out any name, "
    "date or time until it matches."
)


_DOB_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y",
    "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
)


def _parse_dob(value: str | None) -> tuple[int, int, int] | None:
    """Read a date of birth however it arrives, or None.

    The clinic's record holds an ISO date; the caller SAYS theirs out loud and
    Retell transcribes it — "March 7th, 1984", "3/7/1984", "the seventh of March
    1984". Formatting must never be what decides whether someone reaches a
    record, in either direction: too strict locks a patient out of their own
    appointment, too loose lets a wrong date through.
    """
    if not value:
        return None
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", value.strip(), flags=re.I)
    cleaned = cleaned.replace(",", " ").replace(".", " ")
    cleaned = re.sub(r"\b(of|the)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for fmt in _DOB_FORMATS:
        try:
            d = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return (d.year, d.month, d.day)
    return None


def _dob_matches(stored: str | None, offered: str | None) -> bool:
    """True only when both parse AND name the same day."""
    a, b = _parse_dob(stored), _parse_dob(offered)
    return a is not None and a == b


async def _caller_is_verified(
    session, call: Call | None, patient: Patient, args: dict
) -> set[uuid.UUID] | None:
    """What this caller may act on: None = nothing, empty set = everything.

    A non-empty set is a LIMITED grant — only the bookings this call created.
    That distinction is the whole point:

    Booking is deliberately unauthenticated, because a first-time patient must be
    able to make one. But _upsert_patient matches on phone number alone, so
    "booking as a patient" attaches to whatever record already holds that number.
    An attacker who knows only a phone number could therefore book, be recognised
    as "the person who booked on this call", and then cancel or move the
    appointment that person really had — the tool cancels the SOONEST upcoming
    one, which is the victim's, not the decoy.

    The rule was written from the honest case (booked, then changed their mind)
    and passes it. It also passes an attacker performing the same steps, because
    the qualifying fact is one the attacker produces themselves.

    So proving ownership still returns full access; creating something in this
    call now returns access to exactly that, and nothing else.
    """
    requested = normalize_phone(args.get("patient_phone") or "")
    from_number = normalize_phone((call.from_number if call else "") or "")
    if requested and from_number and requested == from_number:
        return set()  # calling from the number on the record — full access
    if _dob_matches(patient.date_of_birth, args.get("patient_dob")):
        return set()  # knows the date of birth — full access
    # Same conversation: this caller already booked as this patient on THIS call,
    # so they are demonstrably the person the record belongs to. Without this, a
    # patient who books and then changes their mind two sentences later gets
    # challenged for a date of birth we may not even hold — the common flow, and
    # the one where a web call has no caller ID to match against.
    if call is not None:
        booked_here = (await session.execute(
            select(Booking.id).where(
                Booking.source_call_id == call.id,
                Booking.patient_id == patient.id,
            )
        )).scalars().all()
        if booked_here:
            return set(booked_here)
    return None


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

        patient = await _find_patient_by_phone(
            session, practice_id, phone, dob=args.get("patient_dob")
        )
        if patient is None or patient is AMBIGUOUS:
            return not_found
        call_row = (await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )).scalar_one_or_none()
        allowed = await _caller_is_verified(session, call_row, patient, args)
        # This tool only DISCLOSES — a name, a date, a time. Having created a
        # booking on this call proves nothing about who the record belongs to, so
        # a limited grant (a non-empty set) is not enough here; ownership must be
        # proved by the calling line or the date of birth.
        if allowed is None or allowed:
            # Deliberately reveals nothing — not even that the record exists.
            record_alert("identity_challenge", f"call={retell_call_id} tool=lookup")
            return {"found": False, "verify_identity": True,
                    "message": _IDENTITY_CHALLENGE}

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
    try:
        await session.flush()
    except IntegrityError:
        # retell_call_id is UNIQUE, so we got here with a row that exists but was
        # invisible: we bound the tenant from agent_id, and the row belongs to a
        # different practice. Raising would 500 the tool mid-call. Roll back to the
        # savepoint, alert, and let the caller carry on without a call row — the
        # tool still answers the patient instead of dying.
        await session.rollback()
        record_alert("call_row_tenant_mismatch", f"call={retell_call_id}")
        logger.warning(
            "call row for %s exists under another practice — agent_id routing "
            "disagreed with the stored call", retell_call_id,
        )
        return None
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


async def _page_clinic_urgent_callback(
    session, practice_id, first_name: str | None, phone: str | None
) -> None:
    """Text the clinic that an urgent callback is waiting.

    The dashboard row is not a page — the sidebar badge only moves while someone
    has the tab open. We tell urgent callers the team will ring back within
    minutes, so the team has to be told without watching a screen. Best-effort:
    a failed page must never fail the callback itself (send_sms already alerts).
    """
    row = (await session.execute(
        select(Practice.name, Practice.transfer_phone_number, Practice.phone_number)
        .where(Practice.id == practice_id)
    )).first()
    if row is None:
        return
    name, transfer_to, main_to = row
    dest = transfer_to or main_to
    if not dest:
        record_alert("urgent_callback_unpageable", f"practice={practice_id}")
        return
    _fire_sms(critical="urgent_callback", coro=page_clinic_urgent_callback(
        to=dest,
        practice_name=name or "Dentovox",
        first_name=first_name,
        patient_phone=phone,
    ))


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
    to_er = _needs_emergency_room(args)
    if to_er:
        urgent = True  # the follow-up is urgent even though the ER comes first

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
            # The agent is about to tell the caller their request was recorded, and
            # nothing was written — we cannot resolve a tenant to write it under.
            # Alert so a human can chase the call before the caller gives up on us.
            record_alert("callback_not_persisted", f"call={retell_call_id}")
            return {
                "status": "er_referral" if to_er else "callback_logged",
                "message": _ER_REFERRAL_MESSAGE if to_er else
                "Your callback request has been recorded; our team will reach out.",
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
        if urgent:
            await _page_clinic_urgent_callback(session, practice_id, first_name, phone)

    logger.info(
        "create_callback_request persisted: call=%s phone=%s urgent=%s er=%s",
        retell_call_id,
        (phone or "")[-4:],
        urgent,
        to_er,
    )
    if to_er:
        # The row is written for follow-up, but what the caller hears is the ER.
        record_alert("medical_er_referral", f"call={retell_call_id}")
        return {"status": "er_referral", "message": _ER_REFERRAL_MESSAGE}
    return {
        "status": "callback_logged",
        "message": "Your callback request has been recorded; our team will reach out.",
    }


async def _handle_transfer_to_human(retell_call_id: str, args: dict) -> dict:
    """Escalate to a human — currently as an URGENT CALLBACK, not a live bridge.

    We do not yet configure Retell's native transfer_call tool, and a `type:
    custom` tool cannot bridge a call: the platform only connects the parties for
    the native tool. This handler used to return "transfer_initiated" and a
    number, so the agent announced "let me connect you" and then nothing
    happened — the caller sat in silence. For someone in pain that is worse than
    making no promise at all.

    Until the native transfer ships, escalation takes the path that demonstrably
    works: an urgent callback row the team sees in the dashboard, and an honest
    line to the caller. The caller's own number is used when the agent hasn't
    collected one, so the request is always actionable.
    """
    if _needs_emergency_room(args):
        # No queue for an airway or an uncontrolled bleed. The callback row below
        # still gets written for follow-up, but what the caller HEARS is the ER.
        record_alert("medical_er_referral", f"call={retell_call_id}")
        honest_reply = {"status": "er_referral", "message": _ER_REFERRAL_MESSAGE}
    else:
        honest_reply = {
            "status": "callback_logged",
            "message": (
                "Of course — I'm flagging this for the team right now and someone "
                "will call you straight back."
            ),
        }
    async with _app_db.async_session_factory() as session:
        practice_id = await _resolve_practice_id_for_call(session, retell_call_id)
        if practice_id is None:
            logger.warning("transfer_to_human: no practice for call=%s", retell_call_id)
            record_alert("escalation_not_persisted", f"call={retell_call_id}")
            return honest_reply
        await set_tenant(session, practice_id)
        call = (await session.execute(
            select(Call).where(Call.retell_call_id == retell_call_id)
        )).scalar_one_or_none()
        # The emergency flow calls create_callback_request(urgent=true) and THEN
        # escalates, so without this the team gets the same caller twice in the
        # queue and two pages.
        if call is not None:
            dup = (await session.execute(
                select(CallbackRequest.id).where(
                    CallbackRequest.call_id == call.id,
                    CallbackRequest.urgent.is_(True),
                    CallbackRequest.status == "pending",
                )
            )).first()
            if dup is not None:
                logger.info(
                    "transfer_to_human: urgent callback already queued for call=%s",
                    retell_call_id,
                )
                return honest_reply
        session.add(CallbackRequest(
            id=uuid.uuid4(),
            practice_id=practice_id,
            call_id=call.id if call else None,
            patient_first_name=args.get("patient_first_name") or args.get("first_name"),
            # The number they're calling from is the reliable one — the agent
            # reaches this tool without having asked for a callback number.
            phone=args.get("patient_phone") or args.get("phone")
                  or (call.from_number if call else None),
            reason=args.get("reason") or "caller asked for a person",
            # A request for a human is urgent by definition — it should sit at
            # the top of the team's queue.
            urgent=True,
            status="pending",
        ))
        await session.commit()
        await _page_clinic_urgent_callback(
            session, practice_id, args.get("patient_first_name"),
            args.get("patient_phone") or (call.from_number if call else None),
        )

    logger.info("transfer_to_human → urgent callback: call=%s reason=%s",
                retell_call_id, args.get("reason"))
    record_alert("human_escalation_requested", f"call={retell_call_id}")
    return honest_reply


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


# The agent is mid-conversation when it calls a tool. If our backend raises — a
# DB blip, an exhausted pool, a bug — FastAPI answers 500 and what the caller
# hears is decided by Retell, not by us: the pre-call path is already wrapped so a
# broken lookup can never stop the phone being answered, and the same instinct
# belongs here. Answer with something the agent can say out loud instead.
_TOOL_FAILURE_REPLY = {
    "error": "backend_unavailable",
    "message": (
        "Tell the caller our system just hiccuped, apologise briefly, take their "
        "name and number, and say the team will call them right back. Do not claim "
        "anything was booked, changed, or cancelled."
    ),
}


async def _dispatch_guarded(
    fn: str, retell_call_id: str, args: dict, agent_id: str | None = None
) -> dict:
    try:
        return await _dispatch_function(fn, retell_call_id, args, agent_id)
    except Exception:  # noqa: BLE001 — dead air is worse than a degraded answer
        logger.exception("tool %s failed for call=%s", fn, retell_call_id)
        record_alert("tool_call_failed", f"fn={fn} call={retell_call_id}")
        return dict(_TOOL_FAILURE_REPLY)


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
        try:
            await _ensure_call_row(retell_call_id, call_obj)
        except Exception:  # noqa: BLE001 — a missing call row must not kill the tool
            logger.exception("ensure_call_row failed for call=%s", retell_call_id)
        return await _dispatch_guarded(
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
        try:
            await _ensure_call_row(payload.get("call_id", ""), call_obj)
        except Exception:  # noqa: BLE001 — a missing call row must not kill the tool
            logger.exception("ensure_call_row failed for call=%s", payload.get("call_id"))
        return await _dispatch_guarded(
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

# Retell substitutes ONLY the keys we send, so a key the prompt references and we
# omit is spoken aloud as a literal "{{callback_eta}}". The emergency branch reads
# these two, so they must never be missing — even when the practice lookup fails.
_VAR_FALLBACKS = {
    "office_status": "open",
    "callback_eta": "shortly",
    # No clinic resolved → no line to transfer to. Empty keeps the prompt on the
    # callback path instead of dialling a literal "{{clinic_transfer_number}}".
    "clinic_transfer_number": "",
}


async def _resolve_practice_for_inbound(agent_id: str | None,
                                        to_number: str | None) -> Practice | None:
    """Which clinic was called — one resolver, shared with the event webhook.

    This used to carry its own copy of the number lookup while the event and
    tool webhook routed on agent id alone. The copy was the correct one, which is
    the worst way for two implementations to disagree: greeting a caller with the
    right clinic's name and then booking them at whichever practice sorted first.
    """
    return await _resolve_practice(agent_id, to_number)


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
        return {"call_inbound": {"dynamic_variables": dict(_VAR_FALLBACKS)}}

    inbound = payload.get("call_inbound") or {}
    agent_id = inbound.get("agent_id")
    to_number = inbound.get("to_number")
    try:
        practice = await _resolve_practice_for_inbound(agent_id, to_number)
        variables = build_dynamic_variables(practice) if practice else dict(_VAR_FALLBACKS)
    except Exception:  # noqa: BLE001 — never block call pickup on a lookup bug
        logger.exception("retell inbound webhook: variable build failed")
        variables = dict(_VAR_FALLBACKS)
    logger.info("retell inbound: vars=%d practice=%s",
                len(variables), "yes" if variables else "none")
    return {"call_inbound": {"dynamic_variables": variables}}
