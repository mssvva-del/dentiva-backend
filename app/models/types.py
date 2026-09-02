"""Custom SQLAlchemy column types."""

from __future__ import annotations

import json
import logging

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
        try:
            return decrypt_pii(value)
        except Exception:  # noqa: BLE001 — a read must not crash on one bad row
            # This one used to raise, which at least got noticed. It is quieter
            # now AND louder: the row reads as absent so a list still renders,
            # and the alert says why the value vanished.
            _warn_undecryptable("EncryptedString")
            return None


_UNDECRYPTABLE_SEEN: set[str] = set()


def _warn_undecryptable(where: str) -> None:
    """Say once, per process, that a stored value would not decrypt."""
    if where in _UNDECRYPTABLE_SEEN:
        return
    _UNDECRYPTABLE_SEEN.add(where)
    try:
        from app.observability.alerts import record_alert

        record_alert(
            "encrypted_value_unreadable",
            f"{where} — wrong ENCRYPTION_KEY, or a row written with an older one",
        )
    except Exception:  # noqa: BLE001 — telling someone must never break a read
        logging.getLogger("dentiva.types").error(
            "encrypted value unreadable in %s", where
        )


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
            # Still None, still no crash — but no longer silent. A row that will
            # not decrypt is indistinguishable from a row that was never written,
            # and the difference matters: pms_credentials reading as None makes a
            # connected clinic look unconnected, the agent falls back to our own
            # calendar, and every screen agrees that nothing is wrong. Rotate the
            # encryption key and that happens to the whole fleet at once.
            #
            # Once per process, not per row: a bad key would otherwise write an
            # alert for every record of every read.
            _warn_undecryptable("EncryptedJSON")
            return None
