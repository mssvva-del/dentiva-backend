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
    """Pretend a bridge has credentials.

    Choosing the bridge moved out of availability and into app.adapters.bridge —
    the PMS a practice runs and the aggregator we reach it through are separate
    facts, and only the second depends on credentials. Defaults here configure
    NexHealth and leave Kolla empty, which is what these tests were written
    against.
    """
    from app.adapters import bridge

    settings = type("S", (), {
        "nexhealth_api_key": "k", "nexhealth_subdomain": "sub",
        "nexhealth_location_id": "1",
        "kolla_api_key": "", "kolla_consumer_id": "", "kolla_connector_id": "",
        "pms_env_practice_id": "",
        **overrides,
    })()
    monkeypatch.setattr(bridge, "get_settings", lambda: settings)


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



# ---------------------------------------------------------------------------
# Which bridge answers. Eaglesoft and Dentrix both refuse to talk to us directly
# — Patterson wants $3–5K to join their programme, Dentrix $5,000 — so we go
# through an aggregator, and there are two at very different prices.
# ---------------------------------------------------------------------------


def test_the_cheaper_bridge_wins_when_both_are_configured(monkeypatch):
    """$19 a location against $75. With both reachable there is nothing to weigh."""
    from app.adapters.bridge import bridge_name

    _configured(monkeypatch, kolla_api_key="k", kolla_consumer_id="consumers/1")
    assert bridge_name(_practice("eaglesoft")) == "kolla"


def test_the_pms_brand_does_not_pick_the_bridge(monkeypatch):
    """A practice says "we run Eaglesoft". Both bridges reach Eaglesoft; which
    one we pay is our decision, not theirs."""
    from app.adapters.bridge import bridge_name

    _configured(monkeypatch)  # NexHealth only
    for system in ("eaglesoft", "dentrix", "open_dental", "curve"):
        assert bridge_name(_practice(system)) == "nexhealth"


def test_no_credentials_means_no_bridge(monkeypatch):
    """A clinic that picked a PMS in onboarding while nothing is wired up is not
    connected. Saying otherwise fails at the worst possible moment — mid-call."""
    from app.adapters.bridge import bridge_name, pms_client_for

    _configured(monkeypatch, nexhealth_api_key="")
    assert bridge_name(_practice("eaglesoft")) is None
    assert pms_client_for(_practice("eaglesoft")) is None


def test_a_practice_with_no_pms_gets_no_bridge_however_well_configured(monkeypatch):
    from app.adapters.bridge import bridge_name

    _configured(monkeypatch, kolla_api_key="k", kolla_consumer_id="consumers/1")
    for system in ("", "none", "mock", "other"):
        assert bridge_name(_practice(system)) is None


# ---------------------------------------------------------------------------
# How long a visit takes. The slot was sized by the clinic's own appointment
# types; the booking stored a flat 60. When those disagree the disagreement is
# silent and it compounds — no race, no error, one quiet afternoon is enough.
# ---------------------------------------------------------------------------


def _practice_with_types(**minutes):
    practice = _practice()
    practice.knowledge_base = {
        "appointment_types": [
            {"name": name, "minutes": mins} for name, mins in minutes.items()
        ]
    }
    return practice


def test_the_clinics_own_length_is_used_not_a_flat_hour():
    from app.services.availability import visit_minutes

    practice = _practice_with_types(cleaning=30, root_canal=90)
    assert visit_minutes(practice, "root_canal") == 90
    assert visit_minutes(practice, "cleaning") == 30


def test_a_procedure_the_clinic_never_configured_falls_back_to_an_hour():
    """Clinics fill this in over time. An unknown procedure must book something
    rather than nothing, and an hour is the safe direction — too long merely
    wastes a slot, too short puts two patients in one chair."""
    from app.services.availability import visit_minutes

    assert visit_minutes(_practice_with_types(cleaning=30), "implant") == 60
    assert visit_minutes(_practice(), "cleaning") == 60


async def test_the_pms_is_asked_for_the_length_of_the_procedure_requested(monkeypatch):
    """It used to ask for a cleaning's length whatever the caller wanted, so a
    clinic could be shown a 30-minute gap for an extraction it books at 90."""
    _configured(monkeypatch)
    practice = _practice_with_types(cleaning=30, extraction=90)
    pms = _FakePMS([])
    await avail.compute_pms_slots(
        practice, procedure="extraction",
        now=datetime(2026, 8, 3, 9, 0, tzinfo=UTC), client=pms,
    )
    assert pms.calls[0]["slot_length"] == 90


# ---------------------------------------------------------------------------
# NEXHEALTH_* and KOLLA_* name ONE clinic's location or linked account. They were
# applied to any practice that had picked a PMS — so the second clinic to finish
# onboarding would have had its agent read the FIRST clinic's openings aloud and,
# with writes enabled, book its patients into the first clinic's chairs.
#
# Nothing would have errored. The two clinics would simply have been one clinic.
# ---------------------------------------------------------------------------


def _practice_with_id(practice_id):
    practice = _practice()
    practice.id = practice_id
    return practice


def test_the_environments_credentials_serve_only_the_practice_they_name(monkeypatch):
    import uuid

    from app.adapters.bridge import bridge_name

    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    _configured(monkeypatch, pms_env_practice_id=str(mine))

    assert bridge_name(_practice_with_id(mine)) == "nexhealth"
    assert bridge_name(_practice_with_id(theirs)) is None, (
        "a second clinic was handed the first clinic's PMS location"
    )


def test_an_unnamed_practice_id_still_serves_a_single_clinic(monkeypatch):
    """Every deployment today has one clinic, and making them all set a variable
    to keep working would be a migration disguised as a safety feature."""
    import uuid

    from app.adapters.bridge import bridge_name

    _configured(monkeypatch, pms_env_practice_id="")
    assert bridge_name(_practice_with_id(uuid.uuid4())) == "nexhealth"


def test_the_wrong_clinic_gets_no_pms_rather_than_someone_elses(monkeypatch):
    """No PMS degrades to our own book, which is honest. Reading somebody else's
    calendar is not, and both look identical to the caller on the phone."""
    import uuid

    from app.adapters.bridge import pms_client_for

    _configured(monkeypatch, pms_env_practice_id=str(uuid.uuid4()))
    assert pms_client_for(_practice_with_id(uuid.uuid4())) is None


# ── the clinic's own credentials, rather than the deployment's ───────────────
#
# Binding the environment to one practice stopped the second clinic reading the
# first clinic's calendar, but left it with no PMS at all and no way to give it
# one short of a redeploy. These pin the column that fixes that — and, more
# importantly, that it cannot be half-set.


def _with_credentials(practice, **fields):
    practice.pms_credentials = fields
    return practice


def test_a_clinic_with_its_own_credentials_ignores_the_environment(monkeypatch):
    """The environment belongs to somebody else and this clinic still gets a
    bridge — that is the whole point of the column."""
    import uuid

    from app.adapters.bridge import bridge_name

    _configured(monkeypatch, pms_env_practice_id=str(uuid.uuid4()))
    theirs = _practice_with_id(uuid.uuid4())
    assert bridge_name(theirs) is None
    _with_credentials(theirs, bridge="kolla", api_key="k", consumer_id="c")
    assert bridge_name(theirs) == "kolla"


def test_the_clinics_own_bridge_wins_over_the_environments(monkeypatch):
    """NexHealth is configured in the environment and this clinic is on Kolla.
    Preferring the environment would authenticate happily against the wrong
    practice's location."""
    from app.adapters.bridge import bridge_name

    _configured(monkeypatch)  # nexhealth in the environment
    practice = _with_credentials(_practice(), bridge="kolla", api_key="k", consumer_id="c")
    assert bridge_name(practice) == "kolla"


def test_half_filled_credentials_are_treated_as_none(monkeypatch):
    """A NexHealth client without a location id does not fail at construction —
    it fails in the middle of a call, which is the same bad news delivered to a
    patient instead of to us. Missing a field must degrade to our own book."""
    import uuid

    from app.adapters.bridge import bridge_name

    _configured(monkeypatch, pms_env_practice_id=str(uuid.uuid4()))
    practice = _practice_with_id(uuid.uuid4())
    for partial in (
        {"bridge": "nexhealth", "api_key": "k", "subdomain": "s"},   # no location
        {"bridge": "kolla", "api_key": "k"},        # neither consumer nor connector
        {"bridge": "carrier-pigeon", "api_key": "k"},
        {"api_key": "k", "subdomain": "s", "location_id": "1"},       # no bridge named
    ):
        _with_credentials(practice, **partial)
        assert bridge_name(practice) is None, f"{partial} was accepted"


def test_a_location_is_enough_because_the_key_is_the_accounts(monkeypatch):
    """One NexHealth key covers every practice connected to our account. Copying
    it into each clinic's row would mean rotating it in as many places as we
    have customers — and the row somebody missed would lose its calendar
    silently, months later, with nothing raised anywhere."""
    import uuid

    from app.adapters.bridge import bridge_name, pms_client_for
    from app.adapters.nexhealth import client as nx

    _configured(monkeypatch, pms_env_practice_id=str(uuid.uuid4()))
    # The client reads its own settings. Given a fake set explicitly rather than
    # whatever happens to be in the environment — a test that reaches real
    # credentials can reach the real account behind them.
    monkeypatch.setattr(nx, "get_settings", lambda: type("S", (), {
        "nexhealth_api_key": "account-key", "nexhealth_subdomain": "acct",
        "nexhealth_location_id": "", "nexhealth_api_url": "https://nexhealth.info",
        "http_connect_timeout": 5.0, "http_read_timeout": 20.0,
        "http_retry_attempts": 1, "http_retry_base_delay": 0.1,
    })())
    practice = _with_credentials(
        _practice_with_id(uuid.uuid4()), bridge="nexhealth", location_id="351939"
    )
    assert bridge_name(practice) == "nexhealth"
    client = pms_client_for(practice)
    assert client._location_id == "351939"     # the clinic's own
    # The key came from somewhere other than this practice's row — which is the
    # whole point. NOT compared against its value: an assertion on a credential
    # prints that credential when it fails, and this one once printed a real
    # production key into a terminal, a log and a chat transcript.
    assert client._api_key
    assert client._api_key != "351939"


def test_a_location_with_no_key_anywhere_is_refused(monkeypatch):
    """A location id addresses nothing without a key. Saying so here beats
    building a client that fails on the first call of the day."""
    import uuid

    from app.adapters.bridge import bridge_name

    _configured(monkeypatch, nexhealth_api_key="", pms_env_practice_id=str(uuid.uuid4()))
    practice = _with_credentials(
        _practice_with_id(uuid.uuid4()), bridge="nexhealth", location_id="351939"
    )
    assert bridge_name(practice) is None


def test_a_clinics_own_key_is_never_mixed_with_the_environments_location(monkeypatch):
    """The dangerous middle: this clinic's api_key with the environment's
    subdomain and location id. That authenticates, and reads the wrong calendar."""
    from app.adapters.bridge import pms_client_for

    _configured(monkeypatch)  # nexhealth: sub / 1
    practice = _with_credentials(
        _practice(), bridge="nexhealth", api_key="theirs",
        subdomain="theirs-sub", location_id="99",
    )
    client = pms_client_for(practice)
    assert client._subdomain == "theirs-sub"
    assert client._location_id == "99"


def test_a_practice_with_no_pms_is_still_refused_a_bridge(monkeypatch):
    """Credentials do not override "this clinic has not connected a PMS" —
    otherwise a stale row books a patient into a system nobody uses."""
    from app.adapters.bridge import bridge_name

    _configured(monkeypatch)
    practice = _with_credentials(
        _practice("none"), bridge="nexhealth", api_key="k", subdomain="s", location_id="1"
    )
    assert bridge_name(practice) is None
