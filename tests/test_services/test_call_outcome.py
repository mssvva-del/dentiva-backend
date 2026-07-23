"""Call-outcome taxonomy classifier — deterministic, re-runnable."""

from __future__ import annotations

from app.services import call_outcome as co
from app.services.call_outcome import classify_outcome


def test_booking_wins_over_everything():
    # even a short, escalated, error-disconnected call is BOOKED if a booking exists
    assert classify_outcome(
        booking_exists=True, call_status="completed",
        disconnection_reason="error_llm", duration_seconds=3,
        escalation_needed=True, call_intent="book_appointment",
    ) == co.BOOKED


def test_voicemail_and_no_answer_and_failed():
    assert classify_outcome(booking_exists=False,
                            disconnection_reason="voicemail_reached") == co.VOICEMAIL
    assert classify_outcome(booking_exists=False,
                            disconnection_reason="dial_no_answer") == co.NO_ANSWER
    assert classify_outcome(booking_exists=False,
                            disconnection_reason="dial_busy") == co.NO_ANSWER
    assert classify_outcome(booking_exists=False,
                            disconnection_reason="error_agent") == co.FAILED
    # missed status with no reason → no_answer
    assert classify_outcome(booking_exists=False, call_status="missed") == co.NO_ANSWER


def test_abandoned_short_call():
    assert classify_outcome(
        booking_exists=False, call_status="completed", duration_seconds=5,
        call_intent="book_appointment",
    ) == co.ABANDONED
    # boundary: exactly the threshold is still abandoned; one over is not
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=co._ABANDON_MAX_SECONDS) == co.ABANDONED
    assert classify_outcome(
        booking_exists=False, call_status="completed",
        duration_seconds=co._ABANDON_MAX_SECONDS + 1, call_intent="question",
    ) == co.INFO_ONLY


def test_emergency_and_transfer():
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=60, call_intent="emergency") == co.EMERGENCY
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=60, escalation_needed=True) == co.TRANSFERRED
    # emergency takes priority over a generic escalation flag
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=60, escalation_needed=True,
                            call_intent="dental_emergency") == co.EMERGENCY


def test_no_booking_vs_info_only():
    # wanted an appointment, left without one → lost booking
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=90, call_intent="book_appointment") == co.NO_BOOKING
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=90, call_intent="reschedule") == co.NO_BOOKING
    # just a question → fine
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=90, call_intent="question") == co.INFO_ONLY
    # no intent known yet (call_ended before call_analyzed) → info_only, not no_booking
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=90) == co.INFO_ONLY


def test_case_insensitive_and_none_safe():
    assert classify_outcome(booking_exists=False, call_status="completed",
                            duration_seconds=60, call_intent="BOOK_APPOINTMENT") == co.NO_BOOKING
    assert classify_outcome(booking_exists=False,
                            disconnection_reason="VOICEMAIL_REACHED") == co.VOICEMAIL
    # all-None besides booking → info_only, never crashes
    assert classify_outcome(booking_exists=False) == co.INFO_ONLY


def test_every_outcome_is_in_all_outcomes():
    # guard: the constants and the ALL set never drift
    for name in ("BOOKED", "TRANSFERRED", "EMERGENCY", "NO_BOOKING", "INFO_ONLY",
                 "ABANDONED", "VOICEMAIL", "NO_ANSWER", "FAILED"):
        assert getattr(co, name) in co.ALL_OUTCOMES
    assert co.FAILURE_OUTCOMES <= co.ALL_OUTCOMES
    assert co.BOOKED not in co.FAILURE_OUTCOMES
    assert co.INFO_ONLY not in co.FAILURE_OUTCOMES
