"""Transcript PII redaction (H4) — mask contact/ID PII in free-text transcripts."""

from __future__ import annotations

from app.utils.redact import redact_pii_text, redact_transcript


def test_masks_email():
    assert redact_pii_text("reach me at jane.doe@example.com ok?") == (
        "reach me at [email] ok?"
    )


def test_masks_phone_various_formats():
    for raw in [
        "call 415-555-1234",
        "call (415) 555-1234",
        "call +1 415 555 1234",
        "call 4155551234",
        "call 415.555.1234",
    ]:
        assert redact_pii_text(raw) == "call [phone]", raw


def test_masks_ssn():
    assert redact_pii_text("ssn is 123-45-6789") == "ssn is [ssn]"


def test_masks_known_name_terms_case_insensitive():
    out = redact_pii_text(
        "Hi, this is Jonathan calling for JONATHAN.", extra_terms=["Jonathan"]
    )
    assert "Jonathan" not in out and "JONATHAN" not in out
    assert out.count("[name]") == 2


def test_single_char_terms_ignored():
    # An initial like "J" must not nuke every J in the text.
    assert redact_pii_text("Just joking, Jim", extra_terms=["J"]) == "Just joking, Jim"


def test_passthrough_when_no_pii():
    assert redact_pii_text("see you at the clinic tomorrow") == (
        "see you at the clinic tomorrow"
    )


def test_none_and_empty_passthrough():
    assert redact_pii_text(None) is None
    assert redact_pii_text("") == ""


def test_redact_transcript_masks_each_turn_without_mutating_input():
    transcript = [
        {"role": "agent", "content": "What's your number?"},
        {"role": "user", "content": "It's 415-555-1234 and bob@x.com"},
        {"role": "user", "text": "I'm Bob"},
        "not-a-dict",
    ]
    out = redact_transcript(transcript, extra_terms=["Bob"])
    assert out[0]["content"] == "What's your number?"
    assert out[1]["content"] == "It's [phone] and [email]"
    assert out[2]["text"] == "I'm [name]"
    # roles preserved; non-dict turn dropped
    assert out[1]["role"] == "user"
    assert len(out) == 3
    # original not mutated
    assert transcript[1]["content"] == "It's 415-555-1234 and bob@x.com"
