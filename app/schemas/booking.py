from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class BookingSummary(BaseModel):
    id: str
    patient_name_redacted: str | None
    # The clinic's own patients, on the clinic's own screen. Masking these was a
    # habit carried from our cross-tenant admin views, where it belongs — here it
    # made the feature useless: a callback request you cannot call back, a
    # waitlist you cannot fill, a booking you cannot ring about. The practice is
    # the covered entity for these people; withholding their number from the
    # front desk protects nobody and stops the work.
    patient_name: str | None = None
    patient_phone: str | None = None
    patient_id: str
    appointment_at: datetime
    duration_minutes: int
    procedure_type: str | None
    provider_name: str | None
    status: str
    source: str
    source_call_id: str | None
    created_at: datetime


class BookingListResponse(BaseModel):
    bookings: list[BookingSummary]
    total: int
    has_more: bool


class BookingStatusUpdate(BaseModel):
    status: str  # confirmed | completed | cancelled | no_show


class BookingEdit(BaseModel):
    """A change made by a person, not by the agent.

    Every field optional: an edit names only what it changes, so moving a time
    cannot silently blank a procedure somebody typed in last week.
    """

    appointment_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    procedure_type: str | None = Field(default=None, max_length=120)
    provider_name: str | None = Field(default=None, max_length=120)


class DashboardToday(BaseModel):
    calls_today: int
    calls_answered_by_ai: int
    calls_missed: int
    bookings_made_today: int
    upcoming_appointments_today: int


class BriefingResponse(BaseModel):
    text: str
    stats: DashboardToday
    peak_hours: list[dict]
    generated_at: datetime
    ai_generated: bool


# ── Analytics schemas ─────────────────────────────────────────────────────────


class DailyStats(BaseModel):
    date: date
    calls_total: int
    calls_answered_by_ai: int
    calls_missed: int
    bookings_created: int
    avg_duration_seconds: int


class WeeklyTotals(BaseModel):
    calls_total: int
    calls_answered_by_ai: int
    calls_missed: int
    bookings_created: int
    ai_answer_rate: float


class WeeklyStatsResponse(BaseModel):
    days: list[DailyStats]
    totals: WeeklyTotals


class HourlyCount(BaseModel):
    hour: int
    count: int


class CallsByHourResponse(BaseModel):
    hours: list[HourlyCount]
    peak_hour: int
    peak_count: int


class ProcedureCount(BaseModel):
    procedure: str
    count: int


class ConversionResponse(BaseModel):
    period_days: int
    calls_total: int
    calls_completed: int
    calls_with_booking_intent: int
    bookings_created: int
    conversion_rate: float
    ai_answer_rate: float
    avg_call_duration_seconds: int
    top_procedures: list[ProcedureCount]


class ROIResponse(BaseModel):
    period_days: int
    calls_handled_by_ai: int
    calls_missed: int
    bookings_by_ai: int
    total_talk_time_minutes: int
    minutes_saved: int
    cost_saved_usd: float
    revenue_protected_usd: float
    ai_answer_rate_pct: float


class ActivityDay(BaseModel):
    date: date
    created: int
    rescheduled: int
    cancelled: int
    no_show: int = 0


class EngagementResponse(BaseModel):
    """SMS-engagement + waitlist funnel over a window, from audit logs.

    Surfaces the V2/V3 features' real-world impact: how many patients joined the
    waitlist, how many got notified of an opening, and how patients responded to
    texts (confirmed / cancelled by reply, recall outreach, opt-outs).
    """

    period_days: int
    waitlist_joined: int
    waitlist_notified: int
    waitlist_conversion_rate: float  # notified / joined
    sms_confirmed: int
    sms_cancelled: int
    recall_sms_sent: int
    sms_opt_outs: int


class ActivityResponse(BaseModel):
    """Appointment lifecycle activity over a window — created / rescheduled /
    cancelled / no-show — sourced from audit logs so reschedules, cancellations
    (etap 1 voice tools) and no-shows become visible alongside new bookings."""

    period_days: int
    created: int
    rescheduled: int
    cancelled: int
    no_show: int = 0
    net_added: int
    reschedule_rate: float
    cancellation_rate: float
    no_show_rate: float = 0.0
    days: list[ActivityDay]
