"""The clinic's real calendar — PMS-backed slots and write-back.

Until now a booking existed only in our database. Availability was computed from
business_hours minus our own bookings, which is blind to walk-ins, the front desk
booking directly, and every other channel a practice uses on day one. These tests
pin the two halves of closing that: offering the PMS's own open times, and
putting the appointment back into the PMS calendar.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.adapters.nexhealth.models import NexHealthSlot
from app.models.practice import Practice
from app.services import availability as avail


class _FakePMS:
    """Stands in for NexHealthClient — only the slot search is exercised here."""

    def __init__(self, slots=None, raises=None):
        self._slots = slots or []
        self._raises = raises
        self.calls: list[dict] = []

    async def find_appointment_slots(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._slots


def _practice(pms_system="open_dental") -> Practice:
    return Practice(
        id=uuid.uuid4(), name="Bridge Dental", timezone="America/New_York",
        pms_system=pms_system,
        business_hours={"mon": {"open": "09:00", "close": "18:00"},
                        "tue": {"open": "09:00", "close": "18:00"},
                        "wed": {"open": "09:00", "close": "18:00"},
                        "thu": {"open": "09:00", "close": "18:00"},
                        "fri": {"open": "09:00", "close": "18:00"}},
    )


def _configured(monkeypatch, **overrides):
    """Pretend the NexHealth adapter has credentials."""
    settings = type("S", (), {
        "nexhealth_api_key": "k", "nexhealth_subdomain": "sub",
        "nexhealth_location_id": "1", **overrides,
    })()
    monkeypatch.setattr(avail, "get_settings", lambda: settings)


def test_a_practice_without_a_pms_is_not_connected(monkeypatch):
    _configured(monkeypatch)
    for system in ("", "none", "mock", "other"):
        assert avail.pms_is_connected(_practice(system)) is False


def test_choosing_a_pms_without_credentials_is_not_connected(monkeypatch):
    """A clinic picks "Open Dental" in onboarding long before anyone wires the
    adapter up. Treating that as connected would fail every single call."""
    _configured(monkeypatch, nexhealth_api_key="")
    assert avail.pms_is_connected(_practice()) is False


def test_connected_when_both_halves_are_true(monkeypatch):
    _configured(monkeypatch)
    assert avail.pms_is_connected(_practice()) is True


async def test_pms_slots_carry_the_ids_needed_to_book_them(monkeypatch):
    """A slot without provider/operatory ids can be spoken but never written back
    — the PMS refuses to create an appointment without them."""
    _configured(monkeypatch)
    now = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)  # Monday 5am New York
    pms = _FakePMS([
        NexHealthSlot(start_time="2026-08-03T10:00:00-04:00",
                      provider_id="prov-9", operatory_id="op-2"),
        NexHealthSlot(start_time="2026-08-03T14:00:00-04:00",
                      provider_id="prov-9", operatory_id="op-3"),
    ])
    slots = await avail.compute_pms_slots(_practice(), now=now, client=pms)
    assert [s.time for s in slots] == ["10:00", "14:00"]
    assert slots[0].prov_num == "prov-9"
    assert slots[0].op_num == "op-2"
    # The search asked the PMS for real dates, not our own book.
    assert pms.calls[0]["start_date"] == "2026-08-03"


async def test_a_time_window_filters_pms_slots_too(monkeypatch):
    _configured(monkeypatch)
    now = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    pms = _FakePMS([
        NexHealthSlot(start_time="2026-08-03T10:00:00-04:00", provider_id="p"),
        NexHealthSlot(start_time="2026-08-03T15:00:00-04:00", provider_id="p"),
    ])
    slots = await avail.compute_pms_slots(
        _practice(), preferred_window="afternoon", now=now, client=pms
    )
    assert [s.time for s in slots] == ["15:00"]


async def test_slots_already_past_are_never_offered(monkeypatch):
    _configured(monkeypatch)
    # 3pm New York; the PMS still lists a 10am slot from this morning.
    now = datetime(2026, 8, 3, 19, 0, tzinfo=UTC)
    pms = _FakePMS([
        NexHealthSlot(start_time="2026-08-03T10:00:00-04:00", provider_id="p"),
        NexHealthSlot(start_time="2026-08-04T10:00:00-04:00", provider_id="p"),
    ])
    slots = await avail.compute_pms_slots(_practice(), now=now, client=pms)
    assert [s.date for s in slots] == ["2026-08-04"]


async def test_a_pms_outage_returns_none_rather_than_no_times(monkeypatch):
    """None tells the caller to fall back to our own book. Returning an empty
    list would look like "the clinic is fully booked for two weeks" and leave the
    patient with nothing."""
    from app.adapters.nexhealth.client import NexHealthUnavailable

    _configured(monkeypatch)
    pms = _FakePMS(raises=NexHealthUnavailable("down"))
    assert await avail.compute_pms_slots(_practice(), client=pms) is None


async def test_a_pms_rejection_also_returns_none(monkeypatch):
    from app.adapters.nexhealth.client import NexHealthError

    _configured(monkeypatch)
    pms = _FakePMS(raises=NexHealthError("bad request"))
    assert await avail.compute_pms_slots(_practice(), client=pms) is None


async def test_at_most_two_slots_per_day_so_the_offer_spreads(monkeypatch):
    """Six openings on one morning is not a choice, it's a list. The agent offers
    two options at a time and they should be on different days."""
    _configured(monkeypatch)
    now = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    pms = _FakePMS([
        NexHealthSlot(start_time=f"2026-08-03T{h:02d}:00:00-04:00", provider_id="p")
        for h in (10, 11, 12, 13)
    ] + [NexHealthSlot(start_time="2026-08-04T10:00:00-04:00", provider_id="p")])
    slots = await avail.compute_pms_slots(_practice(), now=now, client=pms)
    per_day = {}
    for s in slots:
        per_day[s.date] = per_day.get(s.date, 0) + 1
    assert max(per_day.values()) <= 2


# ---------------------------------------------------------------------------
# Retell substitutes ONLY the variables we send. A placeholder the prompt uses
# and nobody fills is read out loud, verbatim, to a patient.
# ---------------------------------------------------------------------------


def test_every_placeholder_the_prompt_uses_is_supplied_somewhere():
    """The prompt and the tool descriptions are the contract; dynamic_vars.py and
    the outbound sender are the two places that fill it. Anything referenced in
    the first and missing from both gets spoken as "{{today}}"."""
    import re
    from pathlib import Path

    voice_repo = Path(__file__).resolve().parents[3] / "dentiva-voice"
    agent_dir = voice_repo / "agents" / "front_desk_v1"
    if not agent_dir.exists():  # the voice repo isn't checked out in CI
        import pytest

        pytest.skip("dentiva-voice not present")

    text = (agent_dir / "system_prompt.md").read_text()
    text += (agent_dir / "functions.yaml").read_text()
    used = set(re.findall(r"\{\{(\w+)\}\}", text))
    # Expanded by the sync script from the environment, never by Retell.
    used.discard("BACKEND_URL")

    backend = Path(__file__).resolve().parents[2] / "app"
    supplied = set()
    for module in ("services/llm/dynamic_vars.py", "services/reactivation/voice.py"):
        supplied |= set(re.findall(r'"(\w+)":', (backend / module).read_text()))

    missing = sorted(used - supplied)
    assert not missing, f"spoken to a patient as literal text: {missing}"
