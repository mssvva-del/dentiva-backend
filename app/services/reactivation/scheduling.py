"""Cadence + quiet-hours scheduling for reactivation outreach (block 5).

Defines WHEN a target gets contacted:

  * Cadence — the multi-touch sequence (SMS → voice → SMS) and the gap before
    each touch, stretched over the campaign window (spec 1.6).
  * Quiet hours — never contact outside 08:00–21:00 in the patient's local time
    (TCPA-friendly; spec 1.5). We use the practice timezone as the patient-local
    proxy (patients are local to the practice).

Pure time math — no DB. The campaign builder uses ``first_touch_at`` to schedule
the first touch; the worker (block 6/7) advances to the next cadence step when a
touch completes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class CadenceStep:
    channel: str       # 'sms' | 'voice'
    delay_days: int    # days after enrollment before this touch fires


# Default cadence: text first (cheap, non-intrusive), then a call a few days
# later, then a final text — spread over ~2 weeks. Tunable per campaign.
_DEFAULT_CADENCE = (
    CadenceStep("sms", 0),
    CadenceStep("voice", 3),
    CadenceStep("sms", 14),
)


@dataclass(frozen=True)
class CampaignConfig:
    cadence: tuple[CadenceStep, ...] = field(default=_DEFAULT_CADENCE)
    quiet_start_hour: int = 8   # earliest local hour we may contact
    quiet_end_hour: int = 21    # first local hour we stop (exclusive)


def _resolve_tz(tz_name: str | None):
    try:
        return ZoneInfo(tz_name) if tz_name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def next_allowed_time(
    now: datetime, tz_name: str | None, start_hour: int, end_hour: int
) -> datetime:
    """The soonest moment we may contact, respecting quiet hours.

    If ``now`` is already inside the window, return it unchanged. If it's before
    the window, push to today's window start; if after, to tomorrow's. Returned
    in UTC. Unknown timezone → UTC (so we still schedule rather than never).
    """
    tz = _resolve_tz(tz_name)
    local = now.astimezone(tz)
    if start_hour <= local.hour < end_hour:
        return now
    target = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if local.hour >= end_hour:
        target = target + timedelta(days=1)
    return target.astimezone(UTC)


def first_touch_at(
    now: datetime, tz_name: str | None, config: CampaignConfig
) -> datetime:
    """When the first cadence touch should fire: enrollment + step-0 delay,
    snapped forward into quiet hours."""
    base = now + timedelta(days=config.cadence[0].delay_days)
    return next_allowed_time(
        base, tz_name, config.quiet_start_hour, config.quiet_end_hour
    )
