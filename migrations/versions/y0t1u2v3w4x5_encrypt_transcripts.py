"""Encrypt calls.transcript_jsonb at rest (JSONB → Fernet bytea)

Call transcripts are the richest PHI we hold (spoken names/phone/DOB) and were
stored as plain JSONB — a DB dump leaked full conversations while patient names
were encrypted. This converts the column to encrypted bytea (EncryptedJSON),
encrypting any existing rows in place.

Revision ID: y0t1u2v3w4x5
Revises: x9s0t1u2v3w4
Create Date: 2026-06-27
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.utils.crypto import decrypt_pii, encrypt_pii

revision: str = 'y0t1u2v3w4x5'
down_revision: str | None = 'x9s0t1u2v3w4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    # Read existing plaintext transcripts BEFORE changing the type (jsonb → text).
    rows = conn.execute(
        sa.text("SELECT id, transcript_jsonb::text FROM calls WHERE transcript_jsonb IS NOT NULL")
    ).fetchall()

    # Swap the column to bytea (encrypted). USING NULL drops the old jsonb payload;
    # we re-insert the encrypted form from the snapshot above.
    op.execute("ALTER TABLE calls ALTER COLUMN transcript_jsonb TYPE bytea USING NULL")

    for row_id, text_val in rows:
        if not text_val:
            continue
        enc = encrypt_pii(text_val)  # already-serialized JSON text
        conn.execute(
            sa.text("UPDATE calls SET transcript_jsonb = :v WHERE id = :id"),
            {"v": enc, "id": row_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, transcript_jsonb FROM calls WHERE transcript_jsonb IS NOT NULL")
    ).fetchall()
    op.execute("ALTER TABLE calls ALTER COLUMN transcript_jsonb TYPE jsonb USING NULL")
    for row_id, blob in rows:
        if blob is None:
            continue
        try:
            plain = decrypt_pii(bytes(blob))
            payload = json.loads(plain)
        except Exception:  # noqa: BLE001
            continue
        conn.execute(
            sa.text("UPDATE calls SET transcript_jsonb = :v WHERE id = :id"),
            {"v": json.dumps(payload), "id": row_id},
        )
