"""encrypt callback_requests.reason and waitlist_entries.notes

Both were plaintext because they read like operational notes. They are not: they
hold what a patient dictated, in their own words — "bleeding since the
extraction", "the swelling got worse overnight". Every other obviously-PHI field
on these tables is already Fernet-encrypted, so a database dump leaked exactly
the sentences describing why the patient called while their name and phone
beside it stayed protected.

Existing rows are re-encrypted in place: read the plaintext, encrypt with the
app's own key, write the ciphertext. Done in Python rather than SQL because the
key lives in the application, and in one pass because these tables are small.

Downgrade decrypts back to plaintext so the migration is genuinely reversible —
it needs the same ENCRYPTION_KEY, which is the point.

Revision ID: o6d7e8f9a0b1
Revises: n5c6d7e8f9a0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.utils.crypto import decrypt_pii, encrypt_pii

revision = "o6d7e8f9a0b1"
down_revision = "n5c6d7e8f9a0"
branch_labels = None
depends_on = None

# (table, column) pairs that move from text to encrypted bytea.
_COLUMNS = (
    ("callback_requests", "reason"),
    ("waitlist_entries", "notes"),
)


def _migrate(encrypting: bool) -> None:
    conn = op.get_bind()
    for table, column in _COLUMNS:
        tmp = f"{column}_enc"
        new_type = sa.LargeBinary() if encrypting else sa.Text()
        op.add_column(table, sa.Column(tmp, new_type, nullable=True))

        rows = conn.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
        ).fetchall()
        convert = encrypt_pii if encrypting else decrypt_pii
        for row_id, value in rows:
            conn.execute(
                sa.text(f"UPDATE {table} SET {tmp} = :v WHERE id = :id"),
                {"v": convert(value), "id": row_id},
            )

        op.drop_column(table, column)
        op.alter_column(table, tmp, new_column_name=column)


def upgrade() -> None:
    _migrate(encrypting=True)


def downgrade() -> None:
    _migrate(encrypting=False)
