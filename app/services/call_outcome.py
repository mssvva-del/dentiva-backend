"""Canonical call-outcome taxonomy + a deterministic classifier.

Before this, outcome was effectively binary: ``booked`` if a booking existed,
else ``info_only``. That collapsed very different endings — a caller who *wanted*
an appointment and left without one, a hang-up after two seconds, a voicemail,
a transfer to a human — all into ``info_only``, which the self-learning QA loop
(app/services/qa) then couldn't tell apart. This module gives every call one of
a fixed set of outcomes, derived from signals we already capture, so QA can find
real failure patterns (a ``no_booking`` cluster is a lost-revenue signal;
``info_only`` is a job well done).

The classifier is a pure function — same inputs, same outcome, no I/O — so it can
run at ``call_ended`` (booking/status/duration known) and again at
``call_analyzed`` (intent/escalation known) to refine the result.
"""

from __future__ import annotations

# ── canonical outcomes ───────────────────────────────────────────────────────
BOOKED = "booked"            # a booking was created for this call — the win
TRANSFERRED = "transferred"  # agent handed off to a human (escalation)
EMERGENCY = "emergency"      # dental emergency surfaced (usually also transferred)
NO_BOOKING = "no_booking"    # caller wanted an appointment, left without one — FAIL
INFO_ONLY = "info_only"      # question answered, no appointment sought — fine
ABANDONED = "abandoned"      # answered but the caller dropped almost immediately
VOICEMAIL = "voicemail"      # reached/left a voicemail, no live conversation
NO_ANSWER = "no_answer"      # rang out / busy — never connected
FAILED = "failed"            # technical failure (dial/agent error)

ALL_OUTCOMES = frozenset({
    BOOKED, TRANSFERRED, EMERGENCY, NO_BOOKING, INFO_ONLY,
    ABANDONED, VOICEMAIL, NO_ANSWER, FAILED,
})

# Outcomes the QA self-learning loop learns from (the agent might have done
# better). Wins (BOOKED) and clean info calls (INFO_ONLY) are excluded; so is
# EMERGENCY, where escalating IS the correct handling. QA also skips any call
# with an empty transcript, so no-live-conversation outcomes cost nothing here.
FAILURE_OUTCOMES = frozenset({
    NO_BOOKING, ABANDONED, FAILED, TRANSFERRED, VOICEMAIL, NO_ANSWER,
})

# Retell disconnection_reason buckets.
_VOICEMAIL_REASONS = frozenset({"voicemail_reached", "voicemail", "machine_detected"})
_NO_ANSWER_REASONS = frozenset({"dial_no_answer", "dial_busy", "dial_failed"})
_ERROR_PREFIXES = ("error", "dial_failed_")

# A "completed" call shorter than this with no booking reads as a hang-up, not a
# real conversation.
_ABANDON_MAX_SECONDS = 12

# Intents that mean the caller was trying to secure/change an appointment. If one
# of these calls ends with no booking, that's a lost booking, not an info call.
_BOOKING_INTENTS = frozenset({
    "book_appointment", "booking", "schedule", "reschedule", "appointment",
})


def classify_outcome(
    *,
    booking_exists: bool,
    call_status: str | None = None,
    disconnection_reason: str | None = None,
    duration_seconds: int | None = None,
    escalation_needed: bool | None = None,
    call_intent: str | None = None,
) -> str:
    """Map post-call signals → one canonical outcome. Pure; safe to re-run."""
    reason = (disconnection_reason or "").lower()
    intent = (call_intent or "").lower()

    # 1. A booking is the unambiguous win, regardless of anything else.
    if booking_exists:
        return BOOKED

    # 2. Never connected to a live conversation.
    if reason in _VOICEMAIL_REASONS:
        return VOICEMAIL
    if reason in _NO_ANSWER_REASONS:
        return NO_ANSWER
    if reason.startswith(_ERROR_PREFIXES):
        return FAILED
    if call_status and call_status not in ("completed", "ongoing", "in_progress"):
        # e.g. "missed" — dialed but not answered.
        return NO_ANSWER

    # 3. Answered, but the caller dropped almost immediately.
    if duration_seconds is not None and duration_seconds <= _ABANDON_MAX_SECONDS:
        return ABANDONED

    # 4. Emergencies and human hand-offs (correct handling, not a booking).
    if intent in ("emergency", "dental_emergency"):
        return EMERGENCY
    if escalation_needed:
        return TRANSFERRED

    # 5. A real conversation that produced no booking. If the caller was trying to
    #    book, that's a lost booking; otherwise it was an informational call.
    if intent in _BOOKING_INTENTS:
        return NO_BOOKING
    return INFO_ONLY
