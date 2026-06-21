"""Fernet key versioning / rotation (C4).

ENCRYPTION_KEY is the primary (encrypt) key; ENCRYPTION_KEYS_OLD holds retired
keys that still decrypt. Rotation is backward-compatible: data written under an
old key keeps decrypting, and rotate_pii() re-encrypts it under the primary.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.utils import crypto
from app.utils.crypto import InvalidToken

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()
KEY_C = Fernet.generate_key().decode()


def _set_keys(monkeypatch, *, primary: str, old: str = ""):
    monkeypatch.setenv("ENCRYPTION_KEY", primary)
    monkeypatch.setenv("ENCRYPTION_KEYS_OLD", old)
    get_settings.cache_clear()
    crypto._multifernet.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches():
    yield
    # Restore the suite's configured key/state after each test.
    get_settings.cache_clear()
    crypto._multifernet.cache_clear()


def test_old_key_data_still_decrypts_after_rotation(monkeypatch):
    _set_keys(monkeypatch, primary=KEY_A)
    blob = crypto.encrypt_pii("Maria Garcia")

    # Rotate: B is now primary, A retired (decrypt-only).
    _set_keys(monkeypatch, primary=KEY_B, old=KEY_A)
    assert crypto.decrypt_pii(blob) == "Maria Garcia"


def test_new_data_uses_primary_key(monkeypatch):
    _set_keys(monkeypatch, primary=KEY_B, old=KEY_A)
    blob = crypto.encrypt_pii("new value")
    # With only B configured (A gone), the new blob still decrypts → B wrote it.
    _set_keys(monkeypatch, primary=KEY_B)
    assert crypto.decrypt_pii(blob) == "new value"


def test_rotate_pii_reencrypts_under_primary(monkeypatch):
    _set_keys(monkeypatch, primary=KEY_A)
    blob = crypto.encrypt_pii("rotate me")

    _set_keys(monkeypatch, primary=KEY_B, old=KEY_A)
    rotated = crypto.rotate_pii(blob)

    # After retiring A entirely, the rotated blob still decrypts (now under B).
    _set_keys(monkeypatch, primary=KEY_B)
    assert crypto.decrypt_pii(rotated) == "rotate me"


def test_unknown_key_raises_invalid_token(monkeypatch):
    _set_keys(monkeypatch, primary=KEY_A)
    blob = crypto.encrypt_pii("secret")
    # Configure a totally unrelated key set → cannot decrypt.
    _set_keys(monkeypatch, primary=KEY_C)
    with pytest.raises(InvalidToken):
        crypto.decrypt_pii(blob)
