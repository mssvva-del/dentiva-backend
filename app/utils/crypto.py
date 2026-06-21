"""PII encryption helpers using Fernet (symmetric, authenticated).

Keys come from env. ``ENCRYPTION_KEY`` is the PRIMARY key — all new ciphertext is
written with it. ``ENCRYPTION_KEYS_OLD`` (comma-separated, optional) holds retired
keys that can still DECRYPT older data during/after a rotation. Internally we use
``MultiFernet([primary, *old])``: it encrypts with the primary and decrypts by
trying every key in order, so rotation is backward-compatible — existing rows keep
decrypting until they're re-encrypted (see ``rotate_pii``).

Encrypted values are stored as ``bytea`` (Python ``bytes``) in Postgres. Fernet
provides integrity, so a tampered ciphertext raises ``InvalidToken`` on decrypt.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

__all__ = ["encrypt_pii", "decrypt_pii", "rotate_pii", "InvalidToken"]


def _as_fernet(key: str) -> Fernet:
    # Fernet accepts str or bytes; normalize to bytes.
    return Fernet(key.encode() if isinstance(key, str) else key)


@lru_cache
def _multifernet() -> MultiFernet:
    settings = get_settings()
    primary = settings.encryption_key
    if not primary:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    old = [k.strip() for k in settings.encryption_keys_old.split(",") if k.strip()]
    # Primary first → it's the one used to encrypt; old keys are decrypt-only.
    return MultiFernet([_as_fernet(primary), *(_as_fernet(k) for k in old)])


def encrypt_pii(value: str) -> bytes:
    """Encrypt a plaintext string to ciphertext bytes (with the primary key)."""
    return _multifernet().encrypt(value.encode("utf-8"))


def decrypt_pii(blob: bytes) -> str:
    """Decrypt ciphertext bytes back to the original string.

    Tries the primary key then any retired keys. Raises ``InvalidToken`` if the
    data was tampered with or no configured key matches.
    """
    return _multifernet().decrypt(bytes(blob)).decode("utf-8")


def rotate_pii(blob: bytes) -> bytes:
    """Re-encrypt an existing ciphertext under the PRIMARY key.

    Decrypts with whichever key matches (incl. retired ones) and re-encrypts with
    the current primary, without exposing the plaintext to the caller. Use in a
    background re-encryption pass after a key rotation so retired keys can
    eventually be removed. Raises ``InvalidToken`` if no key matches.
    """
    return _multifernet().rotate(bytes(blob))
