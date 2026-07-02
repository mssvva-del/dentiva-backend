"""Encrypt calls.from_number/to_number/recording_path + searchable caller hash

Phone numbers and recording pointers in the calls table were plaintext PHI while
patient.phone was encrypted — a DB dump leaked patient numbers + recording URLs.
This encrypts them at rest (Fernet → bytea) and adds caller_number_hmac (a
deterministic, indexed hash of from_number) so the dashboard can still search calls
by number. Existing rows are converted + encrypted in place, in one transaction.

Revision ID: z1u2v3w4x5y6
Revises: y0t1u2v3w4x5
Create Date: 2026-07-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.utils.crypto import decrypt_pii, encrypt_pii, phone_hmac

revision: str = 'z1u2v3w4x5y6'
down_revision: str | None = 'y0t1u2v3w4x5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, from_number, to_number, recording_path FROM calls"
    )).fetchall()

    op.add_column("calls", sa.Column("caller_number_hmac", sa.Text(), nullable=True))
    op.create_index(
        "ix_calls_practice_caller_hmac", "calls", ["practice_id", "caller_number_hmac"]
    )

    # from_number/to_number are NOT NULL — drop the constraint while we swap types,
    # re-add it after backfilling the encrypted values.
    op.alter_column("calls", "from_number", nullable=True)
    op.alter_column("calls", "to_number", nullable=True)
    for col in ("from_number", "to_number", "recording_path"):
        op.execute(f"ALTER TABLE calls ALTER COLUMN {col} TYPE bytea USING NULL")

    for row_id, frm, to, rec in rows:
        conn.execute(
            sa.text(
                "UPDATE calls SET from_number = :f, to_number = :t, "
                "recording_path = :r, caller_number_hmac = :h WHERE id = :id"
            ),
            {
                "f": encrypt_pii(frm) if frm is not None else None,
                "t": encrypt_pii(to) if to is not None else None,
                "r": encrypt_pii(rec) if rec else None,
                "h": phone_hmac(frm),
                "id": row_id,
            },
        )

    op.alter_column("calls", "from_number", nullable=False)
    op.alter_column("calls", "to_number", nullable=False)


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, from_number, to_number, recording_path FROM calls"
    )).fetchall()

    op.alter_column("calls", "from_number", nullable=True)
    op.alter_column("calls", "to_number", nullable=True)
    for col in ("from_number", "to_number", "recording_path"):
        op.execute(f"ALTER TABLE calls ALTER COLUMN {col} TYPE text USING NULL")

    def _dec(v):
        if v is None:
            return None
        try:
            return decrypt_pii(bytes(v))
        except Exception:  # noqa: BLE001
            return None

    for row_id, frm, to, rec in rows:
        conn.execute(
            sa.text(
                "UPDATE calls SET from_number = :f, to_number = :t, recording_path = :r "
                "WHERE id = :id"
            ),
            {"f": _dec(frm) or "unknown", "t": _dec(to) or "unknown",
             "r": _dec(rec), "id": row_id},
        )

    op.alter_column("calls", "from_number", nullable=False)
    op.alter_column("calls", "to_number", nullable=False)
    op.drop_index("ix_calls_practice_caller_hmac", table_name="calls")
    op.drop_column("calls", "caller_number_hmac")
