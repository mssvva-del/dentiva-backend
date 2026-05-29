"""PII encryption helpers using Fernet (symmetric, authenticated).

The key comes from the ENCRYPTION_KEY env var. Encrypted values are stored as
``bytea`` (Python ``bytes``) in Postgres. Fernet provides integrity, so a
tampered ciphertext raises ``InvalidToken`` on decrypt.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

__all__ = ["encrypt_pii", "decrypt_pii", "InvalidToken"]


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    # Fernet accepts str or bytes; normalize to bytes.
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_pii(value: str) -> bytes:
    """Encrypt a plaintext string to ciphertext bytes."""
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_pii(blob: bytes) -> str:
    """Decrypt ciphertext bytes back to the original string.

    Raises ``InvalidToken`` if the data was tampered with or the key is wrong.
    """
    return _fernet().decrypt(bytes(blob)).decode("utf-8")
