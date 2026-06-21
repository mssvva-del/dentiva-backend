"""Helpers to produce non-PHI representations for the frontend."""

from __future__ import annotations

import re

# Patterns for structurally-identifiable PII in free-text transcripts. These
# catch the high-risk, regular formats (contact + government IDs). Names are NOT
# pattern-detectable reliably in free text, so callers pass known names via
# ``extra_terms`` (e.g. the patient's first/last name) to mask those too.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# US phone: optional +1, optional (area), separators ., -, space, or none.
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?"          # optional country code
    r"\(?\d{3}\)?[\s.\-]?"        # area code, maybe parenthesised
    r"\d{3}[\s.\-]?\d{4}"          # prefix + line number
)

EMAIL_MASK = "[email]"
SSN_MASK = "[ssn]"
PHONE_MASK = "[phone]"
NAME_MASK = "[name]"


def redact_pii_text(text: str | None, *, extra_terms: list[str] | None = None) -> str | None:
    """Mask structurally-identifiable PII in a free-text string.

    Replaces emails, US phone numbers and SSNs with stable placeholders, plus any
    caller-supplied ``extra_terms`` (matched whole-word, case-insensitively) —
    use these for known names (patient first/last) which can't be detected by
    shape alone. Returns the input unchanged when there's nothing to mask; passes
    None/empty through untouched. Pure function — no logging of the input.
    """
    if not text:
        return text
    # Email first so a phone-shaped digit run inside an address isn't half-masked.
    out = _EMAIL_RE.sub(EMAIL_MASK, text)
    out = _SSN_RE.sub(SSN_MASK, out)  # before phone: 9-digit IDs aren't phones
    out = _PHONE_RE.sub(PHONE_MASK, out)
    for term in extra_terms or []:
        term = (term or "").strip()
        if len(term) < 2:  # skip empty/initials — too noisy to mask safely
            continue
        out = re.sub(rf"\b{re.escape(term)}\b", NAME_MASK, out, flags=re.IGNORECASE)
    return out


def redact_transcript(
    transcript: list[dict] | None, *, extra_terms: list[str] | None = None
) -> list[dict]:
    """Return a copy of a Retell-style transcript with PII masked in each turn.

    Each turn is a dict; the spoken text lives under ``content`` (Retell) or
    ``text``. Only that field is rewritten — role/timestamps are preserved. The
    input list and its dicts are not mutated.
    """
    redacted: list[dict] = []
    for turn in transcript or []:
        if not isinstance(turn, dict):
            continue
        new_turn = dict(turn)
        for field in ("content", "text"):
            if isinstance(new_turn.get(field), str):
                new_turn[field] = redact_pii_text(new_turn[field], extra_terms=extra_terms)
        redacted.append(new_turn)
    return redacted


def redact_name(first_name: str | None, last_name: str | None) -> str | None:
    """Return "First L." style; the frontend never receives full PHI."""
    if not first_name and not last_name:
        return None
    first = (first_name or "").strip()
    last_initial = (last_name or "").strip()[:1]
    if first and last_initial:
        return f"{first} {last_initial}."
    return first or (f"{last_initial}." if last_initial else None)
