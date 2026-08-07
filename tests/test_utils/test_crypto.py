import pytest

from app.utils.crypto import (
    InvalidToken,
    decrypt_pii,
    encrypt_pii,
    normalize_phone,
    phone_hmac,
)


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


def test_a_fragment_is_not_a_phone_number():
    """The voice agent mishears numbers — it is the single most common thing it
    gets wrong. A fragment that normalizes to a valid-looking key is worse than
    no key at all: two unrelated callers collide onto one patient record, and
    from there each of them can act on the other's appointments."""
    for fragment in ("5", "5.", "555-1234", "(555) 123", "", "  ", "abc"):
        assert normalize_phone(fragment) is None, fragment
        assert phone_hmac(fragment) is None, fragment


def test_two_different_fragments_no_longer_share_a_key():
    assert phone_hmac("5") is None and phone_hmac("5.") is None


def test_the_shapes_a_real_number_arrives_in_all_hash_the_same():
    """Formatting must not split one patient into several records."""
    keys = {phone_hmac(p) for p in
            ("+1 (555) 867-5309", "555-867-5309", "5558675309", "1-555-867-5309")}
    assert len(keys) == 1 and None not in keys
