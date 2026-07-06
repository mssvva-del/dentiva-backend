from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.services.call_routing import ANSWER_MODES


class PracticeMe(BaseModel):
    id: str
    name: str
    timezone: str
    phone_number: str | None
    transfer_phone_number: str | None = None
    pms_system: str
    pms_connected: bool
    languages_enabled: list[str]
    business_hours: dict
    reminders_enabled: bool = True
    # Call routing (ring count).
    answer_mode: str = "overflow"
    rings_before_ai: int = 3
    # Generated clinic-facing forwarding instruction (read-only; from the above).
    forwarding_instruction: str = ""


class PracticeUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    phone_number: str | None = None
    transfer_phone_number: str | None = None
    languages_enabled: list[str] | None = None
    business_hours: dict | None = None
    reminders_enabled: bool | None = None
    answer_mode: str | None = None
    # Rings the clinic line waits before forwarding to AI (1–10).
    rings_before_ai: int | None = Field(default=None, ge=1, le=10)

    @field_validator("answer_mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in ANSWER_MODES:
            raise ValueError(f"answer_mode must be one of {ANSWER_MODES}")
        return v
