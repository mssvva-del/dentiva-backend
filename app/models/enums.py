"""Canonical vocabularies for the fields WE fully control.

Single source of truth so the model CHECK constraint, the API validation, and the
write sites can't drift into three disagreeing lists. Call OUTCOMES live in
app.services.call_outcome (ALL_OUTCOMES) — the same idea, kept next to their
classifier. Fields sourced from an LLM (call_intent, patient_sentiment) are
deliberately NOT constrained here: a CHECK would reject a valid new model output.
"""

from __future__ import annotations

from collections.abc import Iterable

# Booking lifecycle status. The only writers are the Retell webhook (create →
# confirmed, cancel → cancelled) and the dashboard PATCH (staff corrections).
CONFIRMED = "confirmed"
COMPLETED = "completed"
CANCELLED = "cancelled"
NO_SHOW = "no_show"
BOOKING_STATUSES = frozenset({CONFIRMED, COMPLETED, CANCELLED, NO_SHOW})

# Booking.source has a single value today (ai_call); not constrained — a CHECK
# would need a migration every time a new intake path is added.
AI_CALL = "ai_call"


def sql_in(values: Iterable[str]) -> str:
    """`('a', 'b', ...)` for a CHECK/IN clause, sorted for a stable migration diff."""
    return "(" + ", ".join(f"'{v}'" for v in sorted(values)) + ")"
