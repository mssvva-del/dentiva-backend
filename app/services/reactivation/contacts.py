"""Patient-contact intake for custom campaigns (CSV / pasted list / one number).

One tolerant parser feeds one upsert path, so the clinic can hand us anything:
  * a CSV export with headers (first_name,last_name,phone,language,email,…),
  * a headerless one-column file of phone numbers,
  * a pasted multi-line list of numbers,
  * a single number.

Rules:
  * phone is the identity — normalized to E.164 (+1XXXXXXXXXX); a row without a
    valid 10-digit US number is rejected into the report, never guessed.
  * Upsert by phone (via the deterministic phone_hmac): re-uploads update names
    instead of duplicating patients.
  * PHI safety: names/phones land in the ENCRYPTED patients columns (RLS table);
    nothing is logged beyond counts.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.patient import Patient
from app.utils.crypto import normalize_phone, phone_hmac

# Header aliases → canonical field. Case/space tolerant.
_HEADER_MAP = {
    "phone": "phone", "phone_number": "phone", "mobile": "phone", "cell": "phone",
    "number": "phone", "tel": "phone", "telephone": "phone",
    "first_name": "first_name", "firstname": "first_name", "first": "first_name",
    "name": "first_name",
    "last_name": "last_name", "lastname": "last_name", "last": "last_name",
    "surname": "last_name",
    "language": "language", "lang": "language",
    "email": "email", "e-mail": "email",
}

_PHONE_CHARS = re.compile(r"[^\d]")


def _normalize_us_phone(raw: str) -> str | None:
    """→ '+1XXXXXXXXXX' or None. Accepts 10 digits, 11 with leading 1, +1…"""
    digits = _PHONE_CHARS.sub("", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return f"+1{digits}"


@dataclass
class ContactRow:
    phone: str
    first_name: str | None = None
    last_name: str | None = None
    language: str = "en"
    email: str | None = None


@dataclass
class IntakeReport:
    added: int = 0
    updated: int = 0
    invalid: list[str] = field(default_factory=list)   # raw values, capped
    duplicates_in_file: int = 0

    def as_dict(self) -> dict:
        return {
            "added": self.added, "updated": self.updated,
            "invalid_count": len(self.invalid),
            "invalid_samples": self.invalid[:10],
            "duplicates_in_file": self.duplicates_in_file,
        }


def parse_contacts(text: str) -> tuple[list[ContactRow], IntakeReport]:
    """Parse CSV-or-lines into ContactRows + a report of rejects."""
    report = IntakeReport()
    rows: list[ContactRow] = []
    seen: set[str] = set()

    text = (text or "").strip()
    if not text:
        return rows, report

    reader = csv.reader(io.StringIO(text))
    records = [r for r in reader if any((c or "").strip() for c in r)]
    if not records:
        return rows, report

    # Header detection: first row contains a known alias AND no digits-only cell.
    first = [c.strip().lower().replace(" ", "_") for c in records[0]]
    has_header = any(c in _HEADER_MAP for c in first) and not any(
        _normalize_us_phone(c) for c in records[0]
    )
    if has_header:
        cols = [_HEADER_MAP.get(c) for c in first]
        data = records[1:]
    else:
        cols = None
        data = records

    for rec in data:
        if cols:
            fields: dict[str, str] = {}
            for i, val in enumerate(rec):
                key = cols[i] if i < len(cols) else None
                if key and (val or "").strip():
                    fields[key] = val.strip()
            raw_phone = fields.get("phone", "")
            # headered file without a phone column → try any cell that looks like one
            if not raw_phone:
                raw_phone = next((c for c in rec if _normalize_us_phone(c)), "")
        else:
            # No header: find the phone cell; leftovers become first/last name.
            raw_phone = next((c for c in rec if _normalize_us_phone(c)), rec[0])
            rest = [c.strip() for c in rec if c.strip() and c != raw_phone]
            fields = {}
            if rest:
                fields["first_name"] = rest[0]
                if len(rest) > 1:
                    fields["last_name"] = rest[1]

        phone = _normalize_us_phone(raw_phone)
        if not phone:
            report.invalid.append(str(raw_phone)[:40])
            continue
        if phone in seen:
            report.duplicates_in_file += 1
            continue
        seen.add(phone)
        lang = (fields.get("language") or "en").lower()
        rows.append(ContactRow(
            phone=phone,
            first_name=(fields.get("first_name") or None),
            last_name=(fields.get("last_name") or None),
            language="es" if lang.startswith("es") else "en",
            email=(fields.get("email") or None),
        ))
    return rows, report


async def upsert_contacts(
    session: AsyncSession, practice_id: uuid.UUID, rows: list[ContactRow],
    report: IntakeReport,
) -> tuple[list[uuid.UUID], IntakeReport]:
    """Upsert by phone_hmac inside the tenant; returns the patient ids touched."""
    ids: list[uuid.UUID] = []
    now = datetime.now(tz=UTC)
    for row in rows:
        h = phone_hmac(normalize_phone(row.phone))
        existing = (await session.execute(
            select(Patient).where(Patient.practice_id == practice_id,
                                  Patient.phone_hmac == h)
        )).scalars().first()
        if existing is not None:
            # Fill blanks only — a re-upload must not clobber richer PMS data.
            if row.first_name and not existing.first_name:
                existing.first_name = row.first_name
            if row.last_name and not existing.last_name:
                existing.last_name = row.last_name
            if row.email and not existing.email:
                existing.email = row.email
            existing.last_synced_at = now
            ids.append(existing.id)
            report.updated += 1
            continue
        p = Patient(
            id=uuid.uuid4(), practice_id=practice_id,
            pms_external_id=f"upload:{uuid.uuid4().hex[:12]}",
            first_name=row.first_name, last_name=row.last_name,
            phone=row.phone, email=row.email,
            preferred_language=row.language, last_synced_at=now,
        )
        session.add(p)
        await session.flush()
        ids.append(p.id)
        report.added += 1
    return ids, report
