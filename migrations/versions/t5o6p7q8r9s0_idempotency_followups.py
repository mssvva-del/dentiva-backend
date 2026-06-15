"""Idempotency follow-ups: calls.usage_metered_at + processed_webhook_events

Audit §2 remaining items:
  * metering double-count on call_ended redelivery → guard with a per-call
    `usage_metered_at` stamp (meter once).
  * Twilio inbound redelivery double-processing → a generic
    `processed_webhook_events(source, event_id)` dedup table (also reusable for
    other providers later).
(The book_appointment retry guard is pure code — it dedups on the existing
booking's source_call_id, no schema needed.)

Revision ID: t5o6p7q8r9s0
Revises: s4n5o6p7q8r9
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 't5o6p7q8r9s0'
down_revision: str | None = 's4n5o6p7q8r9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Meter-once stamp. NULL = not yet metered.
    op.add_column(
        'calls',
        sa.Column('usage_metered_at', sa.DateTime(timezone=True), nullable=True),
    )

    # Generic webhook-event dedup. Not RLS: it's an infra dedup ledger keyed by
    # the provider's own id, written before/without a tenant context.
    op.create_table(
        'processed_webhook_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),     # e.g. 'twilio_sms'
        sa.Column('event_id', sa.Text(), nullable=False),   # provider's unique id
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'event_id', name='uq_processed_event'),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON processed_webhook_events TO dentiva_app"
    )


def downgrade() -> None:
    op.drop_table('processed_webhook_events')
    op.drop_column('calls', 'usage_metered_at')
