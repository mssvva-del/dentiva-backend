from __future__ import annotations

from pydantic import BaseModel


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


class PracticeUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    phone_number: str | None = None
    transfer_phone_number: str | None = None
    languages_enabled: list[str] | None = None
    business_hours: dict | None = None
    reminders_enabled: bool | None = None
