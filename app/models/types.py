"""Custom SQLAlchemy column types."""

from __future__ import annotations

import json

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from app.utils.crypto import decrypt_pii, encrypt_pii


class EncryptedString(TypeDecorator):
    """Transparently encrypts a Python ``str`` into a Postgres ``bytea`` column.

    Stored as Fernet ciphertext; decrypted on load. ``None`` passes through.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> bytes | None:  # noqa: ANN001
        if value is None:
            return None
        return encrypt_pii(value)

    def process_result_value(self, value: bytes | None, dialect) -> str | None:  # noqa: ANN001
        if value is None:
            return None
        return decrypt_pii(value)


class EncryptedJSON(TypeDecorator):
    """Transparently encrypts a JSON-serializable value (list/dict) into a Postgres
    ``bytea`` column — Fernet ciphertext at rest, native Python object in the app.

    Used for call transcripts: the richest PHI we hold (spoken names/phone/DOB).
    Storing them as plain JSONB means a DB dump leaks full conversations; this keeps
    them encrypted at rest like the other PHI columns. Trade-off: the value can't be
    queried/indexed in SQL (fine — transcripts are read whole, never filtered on).
    ``None`` passes through.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return encrypt_pii(json.dumps(value, separators=(",", ":"), ensure_ascii=False))

    def process_result_value(self, value: bytes | None, dialect):  # noqa: ANN001
        if value is None:
            return None
        try:
            return json.loads(decrypt_pii(value))
        except Exception:  # noqa: BLE001 — a corrupt/legacy row must not crash a read
            return None
