"""Helpers to produce non-PHI representations for the frontend."""

from __future__ import annotations


def redact_name(first_name: str | None, last_name: str | None) -> str | None:
    """Return "First L." style; the frontend never receives full PHI."""
    if not first_name and not last_name:
        return None
    first = (first_name or "").strip()
    last_initial = (last_name or "").strip()[:1]
    if first and last_initial:
        return f"{first} {last_initial}."
    return first or (f"{last_initial}." if last_initial else None)
