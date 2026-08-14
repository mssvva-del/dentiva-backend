"""A booking that reaches the clinic's own calendar, and one that honestly cannot.

The product is sold on the appointment appearing where the front desk works. For
a first-time caller it never did: the person existed only in our database, so
there was nobody in the practice's system to attach an appointment to, and the
slot lived nowhere real until somebody retyped it.

Two rules run through all of this, and they pull in opposite directions:

  * a caller must NEVER lose their appointment because we could not write it
    somewhere else — they hung up believing they are booked;
  * we must never invent a date of birth to get past a required field. It is
    what practices match and de-duplicate people on, and a made-up one is
    indistinguishable from a real one after the call ends.
"""

from __future__ import annotations

from app.adapters.nexhealth.models import NexHealthSlot
from app.observability import alerts
from tests.conftest import seed_practice


class _FakePMS:
    """Stands in for NexHealthClient. Records what it was asked to do."""

    def __init__(self, *, found=None, created="900001"):
        self.found = found
        self.created = created
        self.create_calls: list[dict] = []

    async def find_appointment_slots(self, **kwargs):
        # A connected practice is believed about its own calendar: no PMS slots
        # means no openings, not "fall back to our book". So the fake has to
        # offer one, and it carries the provider and operatory a write-back
        # needs.
        return [NexHealthSlot(
            start_time="2099-11-10T10:00:00-05:00",
            provider_id="prov-9", operatory_id="op-2",
        )]

    async def find_patient_id(self, *, phone):
        return self.found

    async def first_provider_id(self):
        return "prov-1"

    async def create_patient(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.created


async def _connected(monkeypatch, pms):
    """A practice with a working NexHealth bridge."""
    from app.adapters import bridge
    from app.webhooks import retell

    monkeypatch.setattr(bridge, "bridge_name", lambda practice: "nexhealth")
    monkeypatch.setattr(bridge, "pms_client_for", lambda practice: pms)
    monkeypatch.setattr(retell, "pms_is_connected", lambda practice: True)


async def _book(client, call_id, *, dob=None):
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": call_id,
        "call": {"from_number": "+16205551111", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    args = {
        "patient_first_name": "Ann", "patient_last_name": "Lee",
        "patient_phone": "+16205551111", "procedure": "cleaning",
        "preferred_date": "2099-11-10", "preferred_time_window": "morning",
    }
    if dob:
        args["patient_dob"] = dob
    response = await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": call_id,
        "function_name": "book_appointment", "args": args,
    })
    body = response.json()
    assert body.get("booked") is True, body
    return body


async def test_a_returning_caller_is_never_asked_for_anything(
    client, db_session, monkeypatch
):
    """The common case, and the reason to look before creating. Someone already
    in the practice's system needs no date of birth and no extra questions —
    their id is simply adopted."""
    await seed_practice(
        db_session, name="Bridge Dental", clerk_org_id="org_b1", clerk_user_id="u_b1"
    )
    pms = _FakePMS(found="514156368")
    await _connected(monkeypatch, pms)

    assert (await _book(client, "book-known"))["booked"] is True
    assert pms.create_calls == [], "a patient who was already there was created again"


async def test_a_new_caller_who_gave_a_date_of_birth_is_created(
    client, db_session, monkeypatch
):
    await seed_practice(
        db_session, name="Bridge Dental 2", clerk_org_id="org_b2", clerk_user_id="u_b2"
    )
    pms = _FakePMS(found=None)
    await _connected(monkeypatch, pms)

    assert (await _book(client, "book-new", dob="1984-03-02"))["booked"] is True
    assert len(pms.create_calls) == 1
    sent = pms.create_calls[0]
    assert sent["date_of_birth"] == "1984-03-02"
    # The address says "we do not have one" rather than guessing at a real
    # person's inbox. Never a shared placeholder: the practice's own mail would
    # then collapse several patients into one address.
    assert sent["email"].endswith("@no-reply.dentovox.com")


async def test_a_caller_without_a_date_of_birth_still_gets_their_appointment(
    client, db_session, monkeypatch
):
    """THE test. NexHealth will not create a patient without a date of birth, and
    we will not invent one — so this booking cannot reach the practice's system.
    It must still be a booking: the caller has hung up believing they have an
    appointment, and they do."""
    await seed_practice(
        db_session, name="Bridge Dental 3", clerk_org_id="org_b3", clerk_user_id="u_b3"
    )
    pms = _FakePMS(found=None)
    await _connected(monkeypatch, pms)
    alerts._RECENT.clear()

    assert (await _book(client, "book-nodob"))["booked"] is True
    assert pms.create_calls == [], "a patient was invented to satisfy a required field"
    # And it is said out loud, because the front desk now has to add them by hand.
    assert "pms_patient_needs_dob" in alerts.recent_alerts()["by_kind"]


async def test_the_clinics_system_going_down_does_not_lose_the_booking(
    client, db_session, monkeypatch
):
    """Their outage, our patient. The appointment stands and somebody is told."""
    await seed_practice(
        db_session, name="Bridge Dental 4", clerk_org_id="org_b4", clerk_user_id="u_b4"
    )

    class _Broken(_FakePMS):
        async def find_patient_id(self, *, phone):
            raise RuntimeError("NexHealth is down")

    await _connected(monkeypatch, _Broken())
    alerts._RECENT.clear()

    assert (await _book(client, "book-down", dob="1984-03-02"))["booked"] is True
    assert "pms_patient_not_in_pms" in alerts.recent_alerts()["by_kind"]


async def test_a_move_is_offered_from_the_clinics_calendar_not_ours(
    client, db_session, monkeypatch
):
    """Reschedule used to read our own book directly.

    check_availability and book_appointment both prefer the PMS on a connected
    practice — our table knows nothing about walk-ins or the front desk booking
    directly, so on a connected clinic it is not an honest answer. Reschedule
    called the native path, so it would move a patient into a Tuesday 10:00 that
    our table shows empty and the practice's calendar shows occupied, and say so
    confidently.
    """
    practice, _ = await seed_practice(
        db_session, name="Move Bridge", clerk_org_id="org_mb1", clerk_user_id="u_mb1"
    )
    pms = _FakePMS(found="514156368")
    await _connected(monkeypatch, pms)

    # Book first, through the PMS-backed path.
    await _book(client, "mv-book")

    # Now move it. The only slot the PMS offers is the one the fake returns; if
    # the handler were reading our own book it would pick some other time, and
    # would carry no provider id for the write-back.
    await client.post("/webhooks/retell", json={
        "event": "call_started", "call_id": "mv-move",
        "call": {"from_number": "+16205551111", "to_number": "+15559876543",
                 "start_timestamp": 1748563200000},
    })
    body = (await client.post("/webhooks/retell", json={
        "event": "function_call", "call_id": "mv-move",
        "function_name": "reschedule_appointment",
        "args": {"patient_phone": "+16205551111", "new_date": "2099-11-10"},
    })).json()

    assert body["rescheduled"] is True, body
    assert body["appointment"]["time"] == "10:00", (
        "the move was offered from our own book, not the clinic's calendar"
    )
