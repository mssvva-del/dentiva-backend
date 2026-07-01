"""patients.phone_hmac — searchable indexed hash of the encrypted phone

Adds a deterministic HMAC sidecar for the encrypted phone so lookups by number are
a single indexed query instead of an O(n) decrypt-every-row scan (the voice hot
path). The new lookup code queries phone_hmac immediately, so this migration
BACKFILLS existing rows in the same step — no window where old rows are NULL and a
returning caller looks new (avoids duplicate patients). Composite index
(practice_id, phone_hmac) matches the tenant-scoped query shape.

Revision ID: x9s0t1u2v3w4
Revises: w8r9s0t1u2v3
Create Date: 2026-06-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# App crypto: this migration decrypts each phone and hashes it. Runs as the
# migration superuser (bypasses RLS), so it sees every practice's rows.
from app.utils.crypto import decrypt_pii, phone_hmac

revision: str = 'x9s0t1u2v3w4'
down_revision: str | None = 'w8r9s0t1u2v3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("phone_hmac", sa.Text(), nullable=True))
    # Composite: every lookup is `practice_id = ? AND phone_hmac = ?`. Non-unique
    # on purpose — family members legitimately share one phone in one practice.
    op.create_index(
        "ix_patients_practice_phone_hmac", "patients", ["practice_id", "phone_hmac"]
    )

    # Backfill existing rows atomically with the schema change.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, phone FROM patients WHERE phone IS NOT NULL")
    ).fetchall()
    for pid, phone_blob in rows:
        try:
            plain = decrypt_pii(phone_blob)
            h = phone_hmac(plain)
        except Exception:  # noqa: BLE001 — a stray undecryptable row must not fail the migration
            h = None
        if h is not None:
            conn.execute(
                sa.text("UPDATE patients SET phone_hmac = :h WHERE id = :id"),
                {"h": h, "id": pid},
            )


def downgrade() -> None:
    op.drop_index("ix_patients_practice_phone_hmac", table_name="patients")
    op.drop_column("patients", "phone_hmac")
