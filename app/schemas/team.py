"""Team management schemas (Platform Iter 1, Phase B3)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Clinic roles an owner/manager may assign. 'owner' included so ownership can be
# transferred; the last-owner guard prevents leaving a practice ownerless.
AssignableRole = Literal["owner", "manager", "staff", "viewer"]

# Pragmatic email check (avoids the optional email-validator dependency). Good
# enough to reject obvious garbage; Clerk does the authoritative validation when
# it sends the invitation.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MemberOut(BaseModel):
    id: str
    email: str
    role: str
    status: str
    is_self: bool


class InvitationOut(BaseModel):
    id: str
    email: str
    role: str
    status: str
    created_at: datetime
    expires_at: datetime | None


class InviteRequest(BaseModel):
    email: str
    role: AssignableRole

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class RoleUpdateRequest(BaseModel):
    role: AssignableRole = Field(...)
