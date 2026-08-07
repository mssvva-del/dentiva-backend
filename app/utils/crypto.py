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

import hashlib
import hmac
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

__all__ = [
    "encrypt_pii",
    "decrypt_pii",
    "rotate_pii",
    "InvalidToken",
    "normalize_phone",
    "phone_hmac",
]


# --------------------------------------------------------------------------- #
# Deterministic phone hash — a SEARCHABLE sidecar for the encrypted phone.
#
# Fernet ciphertext is non-deterministic, so an encrypted phone can't be indexed
# or queried — the old code loaded every patient in a practice and decrypted each
# to find one by number (O(n) + crypto on the voice hot path). Instead we store a
# stable HMAC-SHA256 of the NORMALIZED phone in an indexed column: lookups become a
# single indexed equality, no decryption. The HMAC key is derived from the primary
# encryption key so no new secret is needed, and the raw phone is not recoverable
# from the hash (keyed + one-way).
# --------------------------------------------------------------------------- #
@lru_cache
def _phone_hash_key() -> bytes:
    # Derived from the PRIMARY ENCRYPTION_KEY (domain-separated with a prefix so it
    # is not the Fernet key). CAVEAT: unlike decryption there is no _OLD fallback —
    # if ENCRYPTION_KEY is rotated, every stored phone_hmac becomes stale, so a key
    # rotation MUST be followed by re-running scripts/backfill_phone_hmac.py.
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set — cannot derive phone hash key")
    return hashlib.sha256(("phone-hmac:" + key).encode("utf-8")).digest()


def normalize_phone(phone: str | None) -> str | None:
    """Reduce a phone to comparable digits so formatting differences don't cause
    misses. Strips non-digits; drops a US leading '1' so +1XXXXXXXXXX, 1XXXXXXXXXX
    and (XXX) XXX-XXXX all hash the same. None if empty. US-centric (this is a
    US-only dental product) — no general country-code handling."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # A US number has ten digits. Anything shorter is not a phone number, it is
    # what a speech model produced when it could not hear one — "555-1234", or a
    # bare "5". Those used to become valid keys, so two unrelated callers whose
    # fragments normalized alike landed on ONE patient record and shared a chart.
    # None means "no usable key": the caller is treated as someone we don't know,
    # which is the honest answer. Ten-digit numbers hash exactly as before, so
    # every correctly-stored record keeps matching.
    return digits if len(digits) == 10 else None


def phone_hmac(phone: str | None) -> str | None:
    """Stable HMAC-SHA256 of the normalized phone (hex), or None. Deterministic
    across the fleet → storable + queryable on an index."""
    norm = normalize_phone(phone)
    if norm is None:
        return None
    return hmac.new(_phone_hash_key(), norm.encode("utf-8"), hashlib.sha256).hexdigest()


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
    # WHY order is critical: MultiFernet ALWAYS encrypts with the first key and
    # tries the rest only for decrypt. So primary MUST be first — put an old key
    # first by mistake and new data gets written under a retired key.
    # To rotate: generate a new key, move the current ENCRYPTION_KEY into
    # ENCRYPTION_KEYS_OLD, set the new key as ENCRYPTION_KEY (decrypt of old data
    # keeps working; re-encrypt lazily via rotate_pii before dropping the old key).
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
