from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_current_practice, get_current_user, get_tenant_db
from app.models.audit_log import AuditLog
from app.models.practice import Practice
from app.models.user import User
from app.schemas.practice import PracticeMe, PracticeUpdate

router = APIRouter(prefix="/api/practice", tags=["practice"])


def _build_practice_me(practice: Practice) -> PracticeMe:
    pms_connected = get_settings().pms_adapter != "mock" and bool(
        practice.pms_credentials_secret_key
    )
    return PracticeMe(
        id=str(practice.id),
        name=practice.name,
        timezone=practice.timezone,
        phone_number=practice.phone_number,
        pms_system=practice.pms_system,
        pms_connected=pms_connected,
        languages_enabled=list(practice.languages_enabled),
        business_hours=practice.business_hours,
    )


@router.get("/me", response_model=PracticeMe)
async def practice_me(practice: Practice = Depends(get_current_practice)) -> PracticeMe:
    # In weekend mode the mock PMS is always "connected" conceptually; a real
    # connection is established in Iter 2. Report based on configured adapter.
    return _build_practice_me(practice)


@router.patch("/me", response_model=PracticeMe)
async def update_practice_me(
    payload: PracticeUpdate,
    practice: Practice = Depends(get_current_practice),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
) -> PracticeMe:
    """Partially update practice settings. Only provided (non-None) fields are changed."""
    # Re-fetch within the tenant-bound session so we can mutate and commit.
    db_practice = (
        await db.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()

    changed_fields: list[str] = []

    if payload.name is not None and payload.name != db_practice.name:
        db_practice.name = payload.name
        changed_fields.append("name")

    if payload.phone_number is not None and payload.phone_number != db_practice.phone_number:
        db_practice.phone_number = payload.phone_number
        changed_fields.append("phone_number")

    if payload.timezone is not None and payload.timezone != db_practice.timezone:
        db_practice.timezone = payload.timezone
        changed_fields.append("timezone")

    if (
        payload.languages_enabled is not None
        and list(payload.languages_enabled) != list(db_practice.languages_enabled)
    ):
        db_practice.languages_enabled = list(payload.languages_enabled)
        changed_fields.append("languages_enabled")

    if (
        payload.business_hours is not None
        and payload.business_hours != db_practice.business_hours
    ):
        db_practice.business_hours = payload.business_hours
        changed_fields.append("business_hours")

    if changed_fields:
        audit = AuditLog(
            id=uuid.uuid4(),
            practice_id=db_practice.id,
            user_id=user.id,
            action="practice_updated",
            resource_type="practice",
            resource_id=db_practice.id,
            audit_metadata={"changed_fields": changed_fields},
        )
        db.add(audit)

    await db.commit()
    await db.refresh(db_practice)
    return _build_practice_me(db_practice)
