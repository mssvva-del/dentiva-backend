"""Kolla Unify — the PMS bridge for practices that do not run Open Dental.

Our first customer runs Eaglesoft. Integrating Eaglesoft directly costs $3–5K to
join Patterson's partner programme, the same wall Dentrix put up at $5,000, so
the practical routes are aggregators: NexHealth at $75 per location per month, or
Kolla at a listed $19.

The important difference from NexHealth is not price, it is that **Kolla has no
availability endpoint**. NexHealth answers "what times are free?" directly. Kolla
answers two narrower questions — when is this room open, and what is already
booked in it — and expects the caller to subtract one from the other. That is the
whole of ``find_appointment_slots`` below, and it is why this adapter is longer
than the one it sits beside.

Shapes here come from Kolla's published reference, not from a live connector:
Eaglesoft has to be enabled per account by their support and ours is not enabled
yet. Everything marked UNVERIFIED is a documented shape we have not yet seen a
real response for. The project rule applies — a figure or a shape is either
measured or assumed, and saying which is the difference between a plan and a
guess.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import httpx

from app.adapters.nexhealth.models import PmsAppointment, PmsSlot
from app.config import get_settings

logger = logging.getLogger("dentiva.kolla")

KOLLA_API = "https://unify.kolla.dev/dental/v1"
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class KollaError(Exception):
    """Kolla rejected the request — our fault or theirs, but not transient."""


class KollaUnavailable(Exception):
    """Kolla could not be reached. Distinct from KollaError because the caller
    falls back to our own book rather than telling a patient we are full."""


def _hhmm(value: str) -> time | None:
    try:
        hour, minute = value.split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def _iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class KollaClient:
    """One Kolla connector, one practice.

    ``consumer_id`` identifies the practice inside our connector — Kolla calls a
    connected customer a "linked account" and addresses it with that header.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        connector_id: str | None = None,
        consumer_id: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.kolla_api_key
        self._connector_id = connector_id or settings.kolla_connector_id
        self._consumer_id = consumer_id or settings.kolla_consumer_id
        self._base_url = (base_url or KOLLA_API).rstrip("/")
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Either header identifies whose data we are asking for. consumer-id is
        # the practice; connector-id alone would span every practice on the
        # connector, which is never what a patient-facing call wants.
        if self._consumer_id:
            headers["consumer-id"] = self._consumer_id
        elif self._connector_id:
            headers["connector-id"] = self._connector_id
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self._api_key:
            raise KollaError("KOLLA_API_KEY not set")
        if not (self._consumer_id or self._connector_id):
            raise KollaError("neither KOLLA_CONSUMER_ID nor KOLLA_CONNECTOR_ID set")
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=_TIMEOUT, transport=self._transport
        ) as http:
            try:
                response = await http.request(
                    method, path, headers=self._headers(), **kwargs
                )
            except httpx.HTTPError as exc:
                raise KollaUnavailable(str(exc)) from exc
        if response.status_code >= 500:
            # Their outage, not our request. Retrying or falling back is sane;
            # telling the patient there are no openings is not.
            raise KollaUnavailable(f"Kolla {response.status_code}")
        if response.status_code >= 400:
            raise KollaError(f"Kolla {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise KollaError("Kolla returned a non-JSON body") from exc

    # ── reads ────────────────────────────────────────────────────────────────

    async def list_operatories(self) -> list[str]:
        """Resource names of the treatment rooms, as ``resources/{id}``.

        UNVERIFIED against a live connector. Wrong or empty means we compute no
        slots and fall back to our own book, which is the same behaviour as the
        PMS being down — degraded, never wrong.
        """
        payload = await self._request("GET", "/resources", params={"page_size": 100})
        names = []
        for resource in payload.get("resources") or []:
            name = resource.get("name")
            if name and (resource.get("type") or "operatory").lower() == "operatory":
                names.append(name)
        return names

    async def load_schedule(
        self, resource: str, *, start: date, end: date
    ) -> dict[date, list[tuple[time, time]]]:
        """Open hour blocks per day for one room.

        Kolla returns ``schedule: [{date, blocks: [{start_time, end_time}]}]``
        with times as HH:MM. Anything unparseable is dropped rather than guessed:
        a malformed block offered to a patient becomes an appointment the clinic
        cannot honour.
        """
        payload = await self._request(
            "GET",
            f"/{resource.strip('/')}:loadSchedule",
            params={"filter": f"start_date >= '{start.isoformat()}' "
                              f"AND end_date <= '{end.isoformat()}'"},
        )
        out: dict[date, list[tuple[time, time]]] = {}
        for day in payload.get("schedule") or []:
            try:
                day_date = date.fromisoformat(str(day.get("date")))
            except (ValueError, TypeError):
                continue
            blocks = []
            for block in day.get("blocks") or []:
                opens, closes = _hhmm(block.get("start_time", "")), _hhmm(block.get("end_time", ""))
                if opens and closes and opens < closes:
                    blocks.append((opens, closes))
            if blocks:
                out[day_date] = blocks
        return out

    async def list_appointments(
        self, *, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, str | None]]:
        """Booked intervals in the window, as (start, end, operatory).

        Cancelled and broken appointments free their time up again — treating
        them as busy would hide real openings from every caller.
        """
        payload = await self._request(
            "GET",
            "/appointments",
            params={
                "page_size": 1000,
                "filter": f"start_time > '{start.isoformat()}' "
                          f"AND start_time < '{end.isoformat()}'",
            },
        )
        out = []
        for appointment in payload.get("appointments") or []:
            if appointment.get("cancelled") or appointment.get("broken"):
                continue
            begins, ends = _iso(appointment.get("start_time")), _iso(appointment.get("end_time"))
            if begins and ends:
                out.append((begins, ends, appointment.get("operatory")))
        return out

    # ── the thing NexHealth gives us for free ────────────────────────────────

    async def find_appointment_slots(
        self,
        *,
        start_date: str,
        days: int = 1,
        slot_length: int = 60,
        provider_ids: list[str] | None = None,
        resource_ids: list[str] | None = None,
    ) -> list[PmsSlot]:
        """Open times, computed as room hours minus what is already booked.

        Kolla has no availability endpoint — this is their documented pattern,
        not a workaround. The arithmetic is deliberately conservative: a slot is
        offered only when the whole of it sits inside an open block and touches
        no existing appointment. Offering a time the clinic cannot honour costs
        more than missing one it could.

        ``provider_ids`` is accepted for interface parity with the NexHealth
        client and ignored: Kolla schedules rooms, and which dentist stands in
        the room is the practice's business, not ours.
        """
        try:
            begin = date.fromisoformat(start_date)
        except ValueError as exc:
            raise KollaError(f"bad start_date {start_date!r}") from exc
        finish = begin + timedelta(days=max(1, days))

        rooms = resource_ids or await self.list_operatories()
        if not rooms:
            logger.warning("kolla: no operatories — cannot compute availability")
            return []

        booked = await self.list_appointments(
            start=datetime.combine(begin, time.min, tzinfo=UTC),
            end=datetime.combine(finish, time.min, tzinfo=UTC),
        )
        length = timedelta(minutes=max(5, slot_length))
        slots: list[PmsSlot] = []

        for room in rooms:
            schedule = await self.load_schedule(room, start=begin, end=finish)
            busy = [(s, e) for s, e, op in booked if op is None or op == room]
            for day, blocks in sorted(schedule.items()):
                for opens, closes in blocks:
                    cursor = datetime.combine(day, opens, tzinfo=UTC)
                    day_end = datetime.combine(day, closes, tzinfo=UTC)
                    while cursor + length <= day_end:
                        ends = cursor + length
                        if not any(s < ends and cursor < e for s, e in busy):
                            slots.append(PmsSlot(
                                start_time=cursor.isoformat(),
                                provider_id="",
                                operatory_id=room,
                            ))
                        cursor = ends
        slots.sort(key=lambda s: s.start_time)
        return slots

    # ── writes ───────────────────────────────────────────────────────────────

    async def create_appointment(
        self,
        *,
        patient_pms_id: str,
        start_time: str,
        end_time: str,
        operatory_id: str | None = None,
        note: str | None = None,
        provider_id: str | None = None,
    ) -> PmsAppointment:
        """Put the appointment in the clinic's own calendar.

        Until this succeeds the booking exists only in our database, and the
        front desk double-books over it by lunchtime. ``contact_id`` is Kolla's
        resource name for the patient — ``contacts/{id}`` — so a bare id is
        normalised rather than rejected.
        """
        contact = patient_pms_id if "/" in patient_pms_id else f"contacts/{patient_pms_id}"
        body: dict = {
            "contact_id": contact,
            "start_time": start_time,
            "end_time": end_time,
        }
        if operatory_id:
            body["operatory"] = operatory_id
        if note:
            body["notes"] = note[:1000]
        if provider_id:
            body["providers"] = [{"name": provider_id}]

        payload = await self._request("POST", "/appointments", json=body)
        appointment_id = payload.get("remote_id") or payload.get("name") or ""
        if not appointment_id:
            raise KollaError("Kolla created an appointment with no id we can store")
        return PmsAppointment(
            appointment_id=str(appointment_id),
            start_time=str(payload.get("start_time") or start_time),
        )
