"""A time we say out loud must be the clinic's time, not the database's.

bookings.appointment_at is timestamptz, stored in UTC. Booking and reschedule
answer with the local slot strings they offered, so those were always right.
Everything else formatted the raw column:

    booking.appointment_at.strftime("%H:%M")

which turns a 09:00 New York appointment into "13:00" — and a 17:30 Los Angeles
one into 00:30 the FOLLOWING DAY. That string is what the agent reads aloud when
a patient asks about their appointment, what the cancellation confirms by voice
and by text, what the 24-hour reminder says, and what the waitlist offers to the
next patient. Wrong from the first call, in a confident voice.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.availability import slot_from_utc, slot_to_utc


def test_a_morning_appointment_is_not_read_back_in_the_afternoon():
    stored = slot_to_utc("2026-09-15", "09:00", "America/New_York")
    assert stored.hour == 13, "sanity: the column really is UTC"
    assert slot_from_utc(stored, "America/New_York") == ("2026-09-15", "09:00")


def test_a_late_pacific_appointment_keeps_its_own_day():
    """The worst case. 17:30 in Los Angeles is 00:30 the next day in UTC, so the
    raw column named a time the patient never agreed to AND the wrong date."""
    stored = slot_to_utc("2026-09-15", "17:30", "America/Los_Angeles")
    assert stored.date().isoformat() == "2026-09-16", "sanity: UTC has rolled over"
    assert slot_from_utc(stored, "America/Los_Angeles") == ("2026-09-15", "17:30")


def test_it_survives_the_daylight_saving_change():
    """A booking made in August for a date in November is stored with August's
    offset applied to November's clock — the conversion has to use the offset in
    force on the APPOINTMENT's day, not today's."""
    for date_str, tz in (("2026-01-15", "America/New_York"),
                         ("2026-07-15", "America/New_York")):
        stored = slot_to_utc(date_str, "14:00", tz)
        assert slot_from_utc(stored, tz) == (date_str, "14:00")


def test_an_unknown_timezone_does_not_crash_a_live_call():
    """A practice row with a typo in its timezone must degrade, not raise —
    this runs while a patient is on the line."""
    stored = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    date_str, time_str = slot_from_utc(stored, "Not/AZone")
    assert date_str == "2026-09-15"
    assert time_str == "09:00", "falls back to the default clinic timezone"
