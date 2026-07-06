"""Cache pulled PMS records into our patients table (Phase 1, block 5 sub-step).

Reactivation targets reference our own ``patients`` rows (FK). The pull comes
from the PMS (NexHealth), so before we can enroll a dormant patient we must have
them locally. This upserts the pulled records by (practice_id, pms_external_id)
— creating new patients or refreshing existing ones — and returns the Patient
objects so the campaign builder can read sms_opt_out / id without re-querying.

WHY we never overwrite a present value with an empty pulled one: a dirty/partial
PMS record (blank name/phone) must not wipe data we already hold. We only fill or
refresh with non-empty pulled values.

Flush-only (no commit): the caller wraps the whole campaign build in one
transaction. The tenant must already be bound (set_tenant) by the caller.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.models.patient import Patient


async def upsert_patients(
    session: AsyncSession,
    practice_id: uuid.UUID,
    records: list[PMSReactivationRecord],
    *,
    now: datetime | None = None,
) -> dict[str, Patient]:
    """Upsert pulled records → patients; return {pms_external_id: Patient}."""
    now = now or datetime.now(tz=UTC)
    out: dict[str, Patient] = {}
    for rec in records:
        existing = (
            await session.execute(
                select(Patient).where(
                    Patient.practice_id == practice_id,
                    Patient.pms_external_id == rec.pms_external_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.first_name = rec.first_name or existing.first_name
            existing.last_name = rec.last_name or existing.last_name
            existing.phone = rec.phone or existing.phone
            existing.email = rec.email or existing.email
            # Language preference always reflects the latest pull.
            existing.preferred_language = rec.preferred_language or existing.preferred_language
            existing.last_synced_at = now
            out[rec.pms_external_id] = existing
        else:
            patient = Patient(
                id=uuid.uuid4(),
                practice_id=practice_id,
                pms_external_id=rec.pms_external_id,
                first_name=rec.first_name or None,
                last_name=rec.last_name or None,
                phone=rec.phone or None,
                email=rec.email,
                preferred_language=rec.preferred_language,
                last_synced_at=now,
            )
            session.add(patient)
            await session.flush()
            out[rec.pms_external_id] = patient
    return out
