import pytest

from app.utils.crypto import InvalidToken, decrypt_pii, encrypt_pii


def test_roundtrip():
    original = "Maria Garcia"
    blob = encrypt_pii(original)
    assert isinstance(blob, bytes)
    assert blob != original.encode()
    assert decrypt_pii(blob) == original


def test_ciphertext_is_nondeterministic():
    # Fernet includes a random IV, so two encryptions differ.
    assert encrypt_pii("same") != encrypt_pii("same")


def test_tampering_detected():
    blob = bytearray(encrypt_pii("+15551234567"))
    blob[-1] ^= 0x01  # flip a bit
    with pytest.raises(InvalidToken):
        decrypt_pii(bytes(blob))
