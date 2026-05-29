from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.dependencies import get_current_practice
from app.models.practice import Practice
from app.schemas.practice import PracticeMe

router = APIRouter(prefix="/api/practice", tags=["practice"])


@router.get("/me", response_model=PracticeMe)
async def practice_me(practice: Practice = Depends(get_current_practice)) -> PracticeMe:
    # In weekend mode the mock PMS is always "connected" conceptually; a real
    # connection is established in Iter 2. Report based on configured adapter.
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
