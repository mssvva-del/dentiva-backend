"""Bilingual reactivation SMS copy (Phase 1, block 6).

Per spec 2.1: per-language templates, professionally localized — NOT machine
translation on the fly. Each segment gets a slightly different reason line so the
text feels relevant ("you're due for a cleaning" vs "let's finish your
treatment"). Falls back to English for any unknown language.

Kept tiny and pure (no PHI logging, no I/O) so it's trivially testable and the
copy lives in one place for review.
"""

from __future__ import annotations

from app.services.reactivation.segmentation import (
    DROPPED_TREATMENT,
    OVERDUE_RECALL,
)

# Segment-specific reason line, per language.
_REASON = {
    "en": {
        OVERDUE_RECALL: "you're due for a cleaning",
        DROPPED_TREATMENT: "we'd love to help you finish your treatment plan",
        "default": "it's been a while since your last visit",
    },
    "es": {
        OVERDUE_RECALL: "le toca su limpieza dental",
        DROPPED_TREATMENT: "nos encantaría ayudarle a completar su tratamiento",
        "default": "ha pasado un tiempo desde su última visita",
    },
}


def reactivation_sms_body(
    language: str, *, first_name: str | None, practice_name: str, segment: str
) -> str:
    """Compose the reactivation text in the patient's language."""
    lang = "es" if (language or "en").lower().startswith("es") else "en"
    reason = _REASON[lang].get(segment, _REASON[lang]["default"])
    name = first_name if first_name and first_name != "Unknown" else None

    if lang == "es":
        greeting = f"Hola {name}, " if name else "Hola, "
        return (
            f"{greeting}le escribimos de {practice_name}: {reason}. "
            f"Responda a este mensaje o llámenos y le buscamos una cita. "
            f"Responda STOP para no recibir más mensajes."
        )
    greeting = f"Hi {name}, " if name else "Hi, "
    return (
        f"{greeting}it's {practice_name} — {reason}. "
        f"Reply to this text or call us and we'll find a time that works. "
        f"Reply STOP to opt out."
    )


def custom_sms_body(
    language: str, *, first_name: str | None, practice_name: str,
    context: str | None,
) -> str:
    """SMS for a clinic-authored campaign: the clinic's own message wrapped in
    our compliant frame (identification up front, reply CTA, STOP footer).
    The TCPA promo-gate decision happens in the caller (treatment campaigns are
    still gated; attested marketing passes)."""
    lang = "es" if (language or "en").lower().startswith("es") else "en"
    name = first_name if first_name and first_name != "Unknown" else None
    msg = (context or "").strip()

    if lang == "es":
        greeting = f"Hola {name}, " if name else "Hola, "
        base = f"{greeting}le escribimos de {practice_name}."
        if msg:
            base += f" {msg}"
        return (f"{base} Responda a este mensaje o llámenos para agendar. "
                f"Responda STOP para no recibir más mensajes.")
    greeting = f"Hi {name}, " if name else "Hi, "
    base = f"{greeting}it's {practice_name}."
    if msg:
        base += f" {msg}"
    return (f"{base} Reply to this text or call us to schedule. "
            f"Reply STOP to opt out.")
