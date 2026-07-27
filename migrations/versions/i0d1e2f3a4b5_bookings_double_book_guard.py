"""bookings — DB-level double-book guard (partial unique indexes)

App-side availability could double-book under concurrency: two calls offered the
same slot both pass the in-memory overlap check and insert two confirmed rows.
Availability treats the practice as a single resource (one chair; provider is a
label), so the real uniqueness is (practice_id, appointment_at) among confirmed
rows. Plus one call must not create two confirmed bookings.

Both are partial (status='confirmed') so cancelled/completed rows never collide.
Creation will FAIL if existing confirmed rows already violate this — that's the
correct signal to dedupe first, not a reason to widen the index.

Revision ID: i0d1e2f3a4b5
Revises: h9c0d1e2f3a4
Create Date: 2026-07-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'i0d1e2f3a4b5'
down_revision: str | None = 'h9c0d1e2f3a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_bookings_practice_slot_confirmed",
        "bookings",
        ["practice_id", "appointment_at"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed'"),
    )
    op.create_index(
        "uq_bookings_source_call_confirmed",
        "bookings",
        ["source_call_id"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed' AND source_call_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_bookings_source_call_confirmed", table_name="bookings")
    op.drop_index("uq_bookings_practice_slot_confirmed", table_name="bookings")
