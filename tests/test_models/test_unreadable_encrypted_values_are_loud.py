"""A stored value that will not decrypt must not read as "there was nothing".

The two are indistinguishable to every caller, and the difference is the whole
system: pms_credentials reading as None makes a connected clinic look
unconnected, so the agent quietly offers times from our own calendar while every
screen agrees nothing is wrong. Rotate ENCRYPTION_KEY and that happens to the
entire fleet at once, silently.

Reads still must not crash — one unreadable row cannot take down a call list.
So: None, no exception, and an alert.
"""

from __future__ import annotations

import app.models.types as t
from app.observability import alerts


def _reset():
    t._UNDECRYPTABLE_SEEN.clear()
    alerts._RECENT.clear()


def _kinds() -> dict:
    return alerts.recent_alerts()["by_kind"]


def test_undecryptable_json_returns_none_and_says_so():
    _reset()
    col = t.EncryptedJSON()
    # Ciphertext this key cannot open.
    assert col.process_result_value(b"not-fernet-at-all", None) is None
    assert "encrypted_value_unreadable" in _kinds()


def test_undecryptable_string_returns_none_and_says_so():
    _reset()
    col = t.EncryptedString()
    assert col.process_result_value(b"not-fernet-at-all", None) is None
    assert "encrypted_value_unreadable" in _kinds()


def test_it_says_it_once_not_once_per_row():
    # A wrong key means EVERY row of EVERY read fails. One alert per row would
    # bury the rest of the alert list under a single fault.
    _reset()
    col = t.EncryptedJSON()
    for _ in range(50):
        col.process_result_value(b"not-fernet-at-all", None)
    assert _kinds().get("encrypted_value_unreadable") == 1


def test_none_is_still_just_none():
    # A column that was never written is not a fault and must stay quiet.
    _reset()
    assert t.EncryptedJSON().process_result_value(None, None) is None
    assert t.EncryptedString().process_result_value(None, None) is None
    assert _kinds() == {}


def test_a_value_written_with_this_key_round_trips():
    _reset()
    col = t.EncryptedJSON()
    stored = col.process_bind_param({"bridge": "nexhealth", "location_id": "357023"}, None)
    assert col.process_result_value(stored, None) == {
        "bridge": "nexhealth", "location_id": "357023",
    }
    assert _kinds() == {}
