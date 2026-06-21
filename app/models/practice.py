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
    pms_system: Mapped[str] = mapped_column(Text, nullable=False)
    pms_credentials_secret_key: Mapped[str | None] = mapped_column(Text)
    languages_enabled: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{en}"
    )
    business_hours: Mapped[dict] = mapped_column(JSONB, nullable=False)
    retell_agent_id: Mapped[str | None] = mapped_column(Text)
    # Per-practice toggle for the appointment-reminder scheduler. The global
    # REMINDERS_ENABLED env is the master switch (starts the loop); this lets an
    # individual practice opt in/out. Default on so reminders work once enabled
    # globally without extra per-practice setup.
    reminders_enabled: Mapped[bool] = mapped_column(
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
