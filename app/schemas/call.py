from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CallSummary(BaseModel):
    id: str
    direction: str
    from_number: str
    to_number: str
    started_at: datetime
    duration_seconds: int | None
    status: str
    patient_name_redacted: str | None
    patient_id: str | None
    outcome: str | None
    booking_id: str | None
    transcript_available: bool
    call_intent: str | None = None
    patient_sentiment: str | None = None
    escalation_needed: bool | None = None


class CallListResponse(BaseModel):
    calls: list[CallSummary]
    total: int
    has_more: bool


class TranscriptTurn(BaseModel):
    role: str
    text: str
    ts: float | None = None


class CallDetail(BaseModel):
    id: str
    direction: str
    from_number: str
    to_number: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    status: str
    patient_name_redacted: str | None
    patient_id: str | None
    outcome: str | None
    booking_id: str | None
    recording_url: str | None
    transcript: list[TranscriptTurn]
    call_intent: str | None = None
    patient_sentiment: str | None = None
    escalation_needed: bool | None = None


class ActiveCallSummary(BaseModel):
    id: str
    retell_call_id: str | None
    direction: str
    from_number: str
    started_at: datetime
    duration_seconds_so_far: int


class ActiveCallsResponse(BaseModel):
    active_calls: list[ActiveCallSummary]
    count: int
