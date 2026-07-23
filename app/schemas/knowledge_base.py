"""Pydantic models for the clinic knowledge base (practices.knowledge_base JSONB).

The knowledge base captures per-clinic information used to personalise the AI
agent: which providers work here, what appointment types they offer, which
insurances they accept, clinic policies, and how to handle emergencies.

All fields are optional — a clinic fills in what they know. The root model
``KnowledgeBase`` is serialised into ``practices.knowledge_base`` on PUT and
deserialised from it on GET.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Provider(BaseModel):
    """A clinician or hygienist who sees patients at this practice."""

    name: str = Field(..., description="Full name, e.g. 'Dr. Sarah Smith'")
    type: Literal["general", "hygienist", "orthodontist", "surgeon", "other"] | None = None
    # Days of week this provider works (lowercase 3-letter abbreviations).
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] | None = None
    accepts_new: bool | None = Field(
        default=None, description="Whether this provider is accepting new patients"
    )


class AppointmentType(BaseModel):
    """A service offered by the practice (maps directly to a bookable slot)."""

    name: str = Field(..., description="e.g. 'Cleaning', 'Emergency', 'Crown'")
    minutes: int | None = Field(default=None, description="Default slot length in minutes")
    # Provider type that performs this appointment (not a specific doctor name).
    provider_type: Literal["general", "hygienist", "orthodontist", "surgeon", "other"] | None = None
    # Whether this appointment is available for new (never-seen) patients.
    new_patient: bool | None = None


class Policies(BaseModel):
    """Clinic rules surfaced by the agent in response to patient questions."""

    cancellation: str | None = Field(
        default=None,
        description="e.g. '24 hours notice required or $50 fee'",
    )
    late: str | None = Field(
        default=None,
        description="e.g. '15+ min late may be rescheduled'",
    )
    new_patient: str | None = Field(
        default=None,
        description="e.g. 'Arrive 15 min early with ID and insurance card'",
    )
    parking: str | None = Field(
        default=None,
        description="e.g. 'Free lot behind the building'",
    )


class Emergency(BaseModel):
    """How to handle emergency calls (supplements the emergency lock gate)."""

    # Keywords/phrases that signal an emergency to the agent.
    triggers: list[str] | None = Field(
        default=None,
        description="e.g. ['bleeding', 'trauma', 'severe pain', 'swelling']",
    )
    # Action to take when an emergency is detected.
    action: Literal["transfer", "callback", "message"] | None = None
    # Phone number to transfer to (may differ from practice transfer_phone_number).
    on_call_number: str | None = Field(
        default=None,
        description="E.164 number for after-hours or emergency line",
    )


class KnowledgeBase(BaseModel):
    """Root model stored as JSONB in practices.knowledge_base.

    All top-level keys are optional so a clinic can add knowledge incrementally
    without having to fill everything at once. The agent merges what is present
    into its system prompt; missing sections fall back to generic behaviour.
    """

    providers: list[Provider] | None = None
    appointment_types: list[AppointmentType] | None = None
    # Accepted insurance plan names (free-text strings — each clinic names them
    # differently: "Delta Dental", "Delta Dental PPO", etc.).
    insurances: list[str] | None = None
    # Whether the practice accepts patients without insurance.
    self_pay: bool | None = None
    # The clinic's OWN current promotion/special (e.g. "New patient exam + X-rays
    # $99"). The agent mentions it to callers who ask about price / are new / are
    # on the fence. Not insurance, not our platform discount.
    current_offer: str | None = Field(default=None, max_length=300)
    policies: Policies | None = None
    emergency: Emergency | None = None


class KnowledgeBaseResponse(BaseModel):
    """Response envelope returned by GET /api/knowledge-base."""

    knowledge_base: KnowledgeBase | None = None
