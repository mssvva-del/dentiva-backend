"""Call-routing config: AI answer mode + the clinic forwarding instruction.

See RING_COUNT_ASSESSMENT.md for the why. Short version: for overflow/after_hours,
the ring delay is enforced by the CLINIC's carrier call-forwarding — we can't set
it for them (unless we own the number). What we CAN do is store the choice and
generate the exact instruction the clinic applies at onboarding. This module is
the single source of that logic (pure functions — no DB, no I/O).
"""

from __future__ import annotations

FULL_TIME = "full_time"
OVERFLOW = "overflow"
AFTER_HOURS = "after_hours"
ANSWER_MODES = (FULL_TIME, OVERFLOW, AFTER_HOURS)

# Carriers measure forwarding in seconds; a ring is ~6s. Many carriers also
# enforce a practical minimum (~14s) before "no answer" forwarding triggers.
_SECONDS_PER_RING = 6
_MIN_FORWARD_SECONDS = 14


def rings_to_seconds(rings: int) -> int:
    """Convert a ring count to the carrier forwarding delay in seconds (with the
    carrier minimum applied), so the instruction matches what carriers accept."""
    return max(_MIN_FORWARD_SECONDS, rings * _SECONDS_PER_RING)


def forwarding_instruction(*, answer_mode: str, rings_before_ai: int, ai_number: str | None) -> str:
    """The onboarding instruction the clinic follows to route calls to the AI.

    full_time   → publish the AI number as the main line (no forwarding).
    overflow    → conditional forwarding (no-answer + busy) after N rings.
    after_hours → forward to the AI only outside business hours.
    """
    num = ai_number or "your Dentovox number"
    if answer_mode == FULL_TIME:
        return (
            f"Publish {num} as your practice's main line — Dentovox answers every "
            f"call immediately."
        )
    seconds = rings_to_seconds(rings_before_ai)
    if answer_mode == AFTER_HOURS:
        return (
            f"Set your phone line to forward to {num} outside business hours (after "
            f"~{rings_before_ai} rings / {seconds}s when no one answers). Dentovox "
            f"covers nights and weekends; your team takes daytime calls."
        )
    # overflow (default)
    return (
        f"Set Conditional Call Forwarding on your line — 'forward when busy / no "
        f"answer' to {num}, after ~{rings_before_ai} rings ({seconds}s). Your team "
        f"answers first; Dentovox catches every call they can't."
    )
