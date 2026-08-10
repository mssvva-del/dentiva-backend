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


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).first())


def _migrate(encrypting: bool) -> None:
    conn = op.get_bind()

    # Fail fast instead of hanging. Every step here needs ACCESS EXCLUSIVE, and
    # on a zero-downtime platform the PREVIOUS release is still serving traffic
    # while this runs. Without a bound, a blocked ALTER waits silently until the
    # deploy's health-check window expires, the platform rolls back, and the next
    # deploy repeats it — the failure reads as "health-check failure" and says
    # nothing about a lock. Ten seconds turns that into a legible error.
    # Short on purpose. A lock REQUEST queues every new query on the table behind
    # it, so waiting here does not merely delay the deploy — it stalls the live
    # clinic still being served by the previous release. Give up quickly and let
    # scripts/migrate.sh try again in the next gap.
    conn.execute(sa.text("SET lock_timeout = '3s'"))

    for table, column in _COLUMNS:
        tmp = f"{column}_enc"
        new_type = sa.LargeBinary() if encrypting else sa.Text()
        # A previous attempt may have died between adding this column and
        # dropping the original — mid-migration DDL is not rolled back on every
        # platform. Starting over from a clean slate is safe because nothing has
        # read from the temporary column yet.
        if _column_exists(conn, table, tmp):
            op.drop_column(table, tmp)
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
