"""Billing API schemas (Platform Iter 1, Phase D). Money fields are cents."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PlanOut(BaseModel):
    key: str
    name: str
    monthly_price_cents: int
    annual_total_cents: int
    annual_monthly_equivalent_cents: int
    included_minutes: int
    overage_cents_per_min: int
    per_location: bool


class UsageOut(BaseModel):
    period_start: datetime | None
    period_end: datetime | None
    minutes_used: float
    included_minutes: int
    calls_count: int
    sms_count: int
    overage_minutes: float
    overage_cents: int  # estimated cost of overage so far this period


class InvoiceOut(BaseModel):
    id: str
    amount_cents: int
    status: str
    period_start: datetime | None
    period_end: datetime | None
    paid_at: datetime | None
    created_at: datetime


class BillingSummary(BaseModel):
    # None when the practice has no subscription yet (still onboarding / pilot
    # not set up). The frontend shows a "choose a plan" state.
    plan: str | None
    plan_name: str | None
    status: str | None
    billing_cycle: str | None
    mrr_cents: int
    included_minutes: int
    usage: UsageOut
    invoices: list[InvoiceOut]


class CheckoutRequest(BaseModel):
    plan: str           # starter|practice|group
    billing_cycle: str  # monthly|annual


class CheckoutResponse(BaseModel):
    url: str
