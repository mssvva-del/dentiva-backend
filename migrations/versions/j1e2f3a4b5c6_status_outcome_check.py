"""bookings.status + calls.outcome CHECK constraints (audit #3/#8)

Enum-like columns we fully control were free Text with only Python-side validation
in scattered places — a typo or a drifted literal wrote a silently-invalid value
that the QA loop / dashboard filters then missed. Constrain them at the DB, from
the same canonical sets the code uses (app.models.enums / app.services.call_outcome).

LLM-sourced fields (call_intent, patient_sentiment) and calls.status are left
unconstrained on purpose — a CHECK would reject a valid new value. Creation FAILS
if existing rows already violate — the correct signal to clean data first.

Revision ID: j1e2f3a4b5c6
Revises: i0d1e2f3a4b5
Create Date: 2026-07-24
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'j1e2f3a4b5c6'
down_revision: str | None = 'i0d1e2f3a4b5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOKING = "status IN ('cancelled', 'completed', 'confirmed', 'no_show')"
_OUTCOME = (
    "outcome IS NULL OR outcome IN ('abandoned', 'booked', 'emergency', 'failed', "
    "'info_only', 'no_answer', 'no_booking', 'transferred', 'voicemail')"
)


def upgrade() -> None:
    op.create_check_constraint("ck_bookings_status", "bookings", _BOOKING)
    op.create_check_constraint("ck_calls_outcome", "calls", _OUTCOME)


def downgrade() -> None:
    op.drop_constraint("ck_calls_outcome", "calls", type_="check")
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
