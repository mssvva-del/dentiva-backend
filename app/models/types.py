"""Custom SQLAlchemy column types."""

from __future__ import annotations

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
