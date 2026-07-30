"""office_status — the agent must know whether anyone is actually there.

"Our team will call you back in a few minutes" at 11pm is a promise nobody can
keep, and for a caller in pain it means sitting up all night waiting for a phone
that never rings.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.practice import Practice
from app.services.availability import office_status

_TZ = ZoneInfo("America/New_York")
_HOURS = {
    "mon": {"open": "09:00", "close": "18:00"},
    "tue": {"open": "09:00", "close": "18:00"},
    "wed": {"open": "09:00", "close": "18:00"},
    "thu": {"open": "09:00", "close": "18:00"},
    "fri": {"open": "09:00", "close": "18:00"},
    "sat": {"open": "09:00", "close": "13:00"},
}


def _p(hours=None, tz="America/New_York") -> Practice:
    return Practice(name="Test Clinic", timezone=tz, business_hours=hours)


def _at(iso: str):
    return datetime.fromisoformat(iso).replace(tzinfo=_TZ)


def test_open_during_business_hours():
    assert office_status(_p(_HOURS), now=_at("2026-07-30T11:00")) == (
        "open", "in a few minutes",
    )


def test_after_close_points_at_the_next_morning():
    status, eta = office_status(_p(_HOURS), now=_at("2026-07-30T23:00"))
    assert status == "closed"
    assert eta == "first thing tomorrow, from 9 AM"


def test_before_open_says_this_morning():
    status, eta = office_status(_p(_HOURS), now=_at("2026-07-30T07:30"))
    assert status == "closed"
    assert "this morning" in eta and "9 AM" in eta


def test_saturday_afternoon_skips_a_closed_sunday():
    # Sat 2026-08-01 closes at 1pm; Sunday isn't in business_hours at all.
    status, eta = office_status(_p(_HOURS), now=_at("2026-08-01T14:00"))
    assert status == "closed"
    assert eta == "first thing on Monday, from 9 AM"


def test_unknown_hours_are_treated_as_open():
    """Guessing "closed" would tell a caller with a real problem to wait."""
    assert office_status(_p({}), now=_at("2026-07-30T23:00")) == ("open", "shortly")
    assert office_status(_p(None), now=_at("2026-07-30T23:00")) == ("open", "shortly")


def test_clinic_local_time_not_server_time():
    """A Los Angeles clinic is open at 8pm New York time."""
    la = Practice(name="LA", timezone="America/Los_Angeles", business_hours=_HOURS)
    # 20:00 in New York == 17:00 in Los Angeles, inside 9–18 local.
    now = datetime.fromisoformat("2026-07-30T20:00").replace(tzinfo=_TZ)
    assert office_status(la, now=now)[0] == "open"
