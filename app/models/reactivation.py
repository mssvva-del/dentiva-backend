"""Reactivation Engine core models (Phase 1 — flagship).

The Reactivation Engine finds patients who quietly stopped coming and wins them
back. Three tables model that pipeline:

  ReactivationCampaign — one outreach run for a practice + segment (e.g. "lapsed").
  ReactivationTarget   — one patient enrolled in a campaign (the work-queue unit).
  ReactivationTouch    — one contact attempt against a target (SMS or voice call).

WHY all three carry practice_id + RLS: they link to patients (PHI) and hold a
clinic's recall list — a forgotten tenant filter would leak one practice's
dormant-patient list to another (company-ending). RLS is the DB-level backstop.

Block 1 lays the SCHEMA only. Segmentation, value scoring, scheduling and the
actual outreach land in later blocks; statuses below are the vocabulary those
blocks will drive.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class ReactivationCampaign(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "reactivation_campaigns"

    # Tenant key — non-null because it's what the RLS policy filters on.
    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Which dormant cohort this run targets: lapsed | overdue_recall |
    # dropped_treatment. Stored as Text (not a DB enum) so a new segment doesn't
    # need a migration; the service layer validates the value.
    segment: Mapped[str] = mapped_column(Text, nullable=False)
    # Lifecycle: draft → running → paused → completed. The scheduler only works
    # campaigns in "running".
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    # Who launched it (clinic or Dentovox user). Nullable — system-created runs
    # have no human actor.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReactivationTarget(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "reactivation_targets"
    __table_args__ = (
        # A patient is enrolled at most once per campaign — dedup guard so a
        # re-run or a concurrent enroll can't queue the same person twice.
        UniqueConstraint("campaign_id", "patient_id", name="uq_target_campaign_patient"),
    )

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("reactivation_campaigns.id"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id"), nullable=False
    )
    # Denormalized from the campaign so per-target queries don't need a join.
    segment: Mapped[str] = mapped_column(Text, nullable=False)
    # Outreach-queue priority: higher = contacted first. Derived (block 4) from
    # treatment-plan value + hygiene LTV + payer mix. Numeric (not float) because
    # it's money-derived and we sort/compare on it.
    value_score: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0"
    )
    # pending → in_progress → booked | no_answer | declined | opted_out | skipped
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # When the scheduler should make the next touch (cadence). NULL = nothing queued.
    next_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    touches_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Set when outreach books an appointment (the PMS write-back result). Lets ROI
    # tracking join recovered revenue back to the actual booking.
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("bookings.id")
    )


class ReactivationTouch(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "reactivation_touches"

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("practices.id"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("reactivation_targets.id"), nullable=False
    )
    # sms | voice — the channel of this attempt.
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    # Delivery state: queued → sent | failed. Independent of the patient outcome.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    # Patient-side result: booked | no_answer | declined | callback | opt_out |
    # delivered. NULL until known.
    outcome: Mapped[str | None] = mapped_column(Text)
    # Language used for this touch (EN/ES) — honors patient.preferred_language.
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    # Provider correlation id (Retell call_id / Twilio MessageSid): idempotency +
    # tracing a touch back to the external system.
    provider_ref: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
