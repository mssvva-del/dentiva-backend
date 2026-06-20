"""Onboarding wizard request/response schemas (Platform Iter 1, Phase B2).

The wizard is step-based with progress saved on `practices.onboarding_step`:
  1 Clinic · 2 Hours · 3 Phone · 4 PMS · 5 Agent · 6 Plan(billing, Phase D) → live

Each step has its own validated payload so a half-finished practice can resume
exactly where it left off. Validation is strict (fail-closed): a malformed step
is rejected rather than silently coerced.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Languages supported in Iter 1 (English + Spanish; Russian removed).
_ALLOWED_LANGUAGES = {"en", "es"}
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # 24h HH:MM
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


# ── Step 1: Clinic ───────────────────────────────────────────────────────────
class ClinicStep(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("name", "timezone")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


# ── Step 2: Business hours ───────────────────────────────────────────────────
class DayHours(BaseModel):
    open: str
    close: str

    @field_validator("open", "close")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError("time must be HH:MM (24h)")
        return v


class HoursStep(BaseModel):
    business_hours: dict[str, DayHours | None]

    @field_validator("business_hours")
    @classmethod
    def _all_days(cls, v: dict) -> dict:
        missing = [d for d in _DAYS if d not in v]
        if missing:
            raise ValueError(f"missing days: {', '.join(missing)}")
        extra = [k for k in v if k not in _DAYS]
        if extra:
            raise ValueError(f"unknown days: {', '.join(extra)}")
        return v


# ── Step 3: Phone ────────────────────────────────────────────────────────────
class PhoneStep(BaseModel):
    # Iter 1 (Sergio): forward an existing number OR skip (web-call only).
    # No real number provisioning yet.
    mode: Literal["forward", "skip"]
    forward_number: str | None = None
    # Where transfer_to_human routes an emergency/live handoff. Optional and
    # independent of mode; falls back to phone_number on the backend when unset.
    # Collected here so a clinic going live has a working transfer destination
    # instead of a NULL that silently breaks the emergency workflow.
    transfer_number: str | None = None

    @field_validator("forward_number", "transfer_number")
    @classmethod
    def _valid_number(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _E164_RE.match(v):
            raise ValueError("number must be E.164, e.g. +13105551234")
        return v


# ── Step 4: PMS ──────────────────────────────────────────────────────────────
class PmsStep(BaseModel):
    # 'none' = skip for now (mock adapter); real connectors land later.
    pms_system: Literal["open_dental", "nexhealth", "none"]


# ── Step 5: Agent ────────────────────────────────────────────────────────────
class AgentStep(BaseModel):
    agent_name: str = Field(min_length=1, max_length=60)
    voice: str = Field(default="cartesia-Hailey", max_length=80)
    greeting: str | None = Field(default=None, max_length=500)
    languages: list[str] = Field(min_length=1)

    @field_validator("languages")
    @classmethod
    def _valid_languages(cls, v: list[str]) -> list[str]:
        bad = [lang for lang in v if lang not in _ALLOWED_LANGUAGES]
        if bad:
            raise ValueError(
                f"unsupported language(s): {', '.join(bad)} (Iter 1 = en, es)"
            )
        # de-dupe, preserve order
        seen: list[str] = []
        for lang in v:
            if lang not in seen:
                seen.append(lang)
        return seen


# ── State (GET) ──────────────────────────────────────────────────────────────
class OnboardingState(BaseModel):
    practice_id: str
    status: str
    onboarding_step: int          # 1..6 in progress; 0 = complete/live
    complete: bool                # onboarding_step == 0
    # Current values so the wizard can resume pre-filled.
    name: str
    address: str | None
    timezone: str
    business_hours: dict
    phone_number: str | None
    transfer_phone_number: str | None = None
    pms_system: str
    languages_enabled: list[str]
    agent_settings: dict | None
    # Step 6 (billing) is handled in Phase D / by super_admin (pilot). Surfaced
    # so the frontend can show "billing set up separately" rather than a dead step.
    billing_deferred: bool = True
