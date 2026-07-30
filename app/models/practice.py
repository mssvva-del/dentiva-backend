from __future__ import annotations

from sqlalchemy import ARRAY, Boolean, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin, UUIDPKMixin


class Practice(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "practices"

    clerk_org_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Street address (single free-text line). Collected in onboarding step 1;
    # nullable because existing practices predate it and it's not load-bearing.
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="America/New_York"
    )
    phone_number: Mapped[str | None] = mapped_column(Text)
    # Where transfer_to_human routes the live call (front-desk / on-call line).
    # E.164. Falls back to phone_number when unset.
    transfer_phone_number: Mapped[str | None] = mapped_column(Text)
    # How the AI fronts the phone (see RING_COUNT_ASSESSMENT.md):
    #   full_time   — AI is the main line, answers immediately.
    #   overflow    — clinic line rings, forwards to AI when unanswered/busy.
    #   after_hours — forwards to AI only outside business hours.
    # The ring DELAY for overflow/after_hours is enforced by the clinic's carrier
    # forwarding (an onboarding instruction we generate), NOT by us — unless we own
    # the number. This field drives the tariff + the instruction + future Twilio
    # overflow. NOT a DB enum so we can add modes without a migration.
    answer_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="overflow")
    # Rings the clinic line waits before forwarding to AI (overflow/after_hours).
    # ~6s/ring; carriers often enforce a ~14s minimum. Default 3.
    rings_before_ai: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    pms_system: Mapped[str] = mapped_column(Text, nullable=False)
    pms_credentials_secret_key: Mapped[str | None] = mapped_column(Text)
    languages_enabled: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{en}"
    )
    business_hours: Mapped[dict] = mapped_column(JSONB, nullable=False)
    retell_agent_id: Mapped[str | None] = mapped_column(Text)
    # The Dentovox number THIS clinic forwards its own line to. Provisioned per
    # practice (Retell buys it in the clinic's area code) — inbound routing keys
    # off it, so it must be unique. NULL until provisioned; the global
    # RETELL_FROM_NUMBER is the single-tenant fallback while that's the case.
    ai_phone_number: Mapped[str | None] = mapped_column(Text, unique=True)
    # Per-practice toggle for the appointment-reminder scheduler. The global
    # REMINDERS_ENABLED env is the master switch (starts the loop); this lets an
    # individual practice opt in/out. Default on so reminders work once enabled
    # globally without extra per-practice setup.
    reminders_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # Text the clinic when the AI books an appointment. Until a PMS sync exists,
    # a new booking is only visible in our dashboard — a front desk does not watch
    # a dashboard, so the alert is what keeps their day accurate. Default on;
    # a busy practice can turn it off in Settings.
    booking_alerts_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )

    # ── Onboarding + lifecycle (Platform Iter 1, Phase B) ────────────────────
    # Wizard progress. Convention: a NEW practice starts at step 1 and counts up
    # through the 6 wizard steps; 0 means onboarding is COMPLETE (practice is
    # live). Existing/demo practices default to 0 (already set up) so this
    # migration doesn't drop them back into the wizard.
    onboarding_step: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # Lifecycle status. One of:
    #   onboarding | trial | pilot | active | suspended | cancelled
    # Drives gating (e.g. block dashboard until onboarding done) and billing.
    # Existing practices default to 'active'; the webhook creates new ones as
    # 'onboarding'. NOT an enum type so super_admin can add states without a
    # migration; validated in the service layer.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active", default="active"
    )
    # Stripe customer handle (set in Phase D at checkout). Nullable until billed.
    stripe_customer_id: Mapped[str | None] = mapped_column(Text)
    # Per-practice agent personalization captured in onboarding step 5
    # ({agent_name, voice, greeting}). Stored as JSONB so we can evolve the shape
    # without a migration. NOT yet wired to the live Retell agent (still a single
    # shared agent in Iter 1); ready for when agents are parameterized per
    # practice. Nullable until the wizard fills it.
    agent_settings: Mapped[dict | None] = mapped_column(JSONB)
    # Clinic knowledge base: providers, appointment types, insurances, policies,
    # emergency protocol. Stored as JSONB (migration u6p7q8r9s0t1). The AI agent
    # injects this into its system prompt to behave as *this clinic's* agent.
    # Nullable — practices that haven't filled it in yet fall back to generic
    # agent behaviour.
    knowledge_base: Mapped[dict | None] = mapped_column(JSONB)
