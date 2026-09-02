"""Real Open Dental REST client (Open Dental Step 2).

Implements PMSAdapter against Open Dental's "The API" REST service. NOT yet wired
to a live clinic — Step 2 is code + mocked-HTTP tests only; PMS_ADAPTER stays
'mock' globally and the real path is opted into per practice (Step 3+).

Auth (confirmed from Open Dental docs):
    Authorization: ODFHIR {DeveloperKey}/{CustomerKey}
  * DeveloperKey  — ours, one for all of Dentiva (from the Developer Portal).
  * CustomerKey   — per clinic (Setup > Advanced Setup > API), passed in here so
    one client instance serves one clinic. Stored encrypted per practice.

Endpoint shapes are taken from the public API docs (apiappointments.html etc.).
Fields marked "TO VERIFY" are best-guess and must be confirmed against a sandbox/
clinic once we have a CustomerKey (see _docs/OPEN_DENTAL_PLAN.md). Because every
HTTP call goes through an injectable httpx transport, all of this is unit-tested
against mocked Open Dental responses without touching the network.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from app.adapters.nexhealth.models import PmsSlot
from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSOperatory,
    PMSPatient,
    PMSProvider,
)
from app.config import get_settings
from app.observability.alerts import record_alert
from app.utils.resilience import make_timeout, retry_async

logger = logging.getLogger("dentiva.pms.open_dental")

# Open Dental datetime wire format, e.g. "2026-06-10 09:00:00".
_OD_DT = "%Y-%m-%d %H:%M:%S"


class PMSError(Exception):
    """A non-recoverable PMS API error (4xx) — surfaced to the caller so the
    voice flow can fall back to a callback instead of falsely confirming."""


class PMSUnavailable(Exception):
    """Transient PMS failure (5xx / network / timeout) — caller may retry or fall
    back. Kept distinct from PMSError so the flow can treat them differently."""


def _to_od_datetime(iso: str) -> str:
    """Our ISO datetime → Open Dental's 'YYYY-MM-DD HH:MM:SS'."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.strftime(_OD_DT)


def _pattern_for(duration_minutes: int) -> str:
    """Open Dental 'Pattern' = one char per 5 minutes ('/' = provider time).
    A 60-min appt → 12 chars. Used when creating/rescheduling so the block has a
    length; the office can refine it later."""
    return "/" * max(1, duration_minutes // 5)


def _digits(value: str) -> str:
    """Just the digits, last ten. "+1 (555) 010-0199" and "5550100199" are the
    same number written by two different people."""
    return "".join(ch for ch in str(value) if ch.isdigit())[-10:]


class OpenDentalClient(PMSAdapter):
    def __init__(
        self,
        *,
        developer_key: str | None = None,
        customer_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> None:
        s = get_settings()
        self._dev_key = developer_key or s.open_dental_dev_key
        self._customer_key = customer_key or s.open_dental_customer_key
        self._base_url = (base_url or s.open_dental_api_url).rstrip("/")
        # Injectable transport so tests stub responses with httpx.MockTransport.
        self._transport = transport
        # Split connect/read timeout: a dead host fails fast, a slow-but-alive one
        # gets the longer read budget.
        self._timeout = timeout or make_timeout(s.http_connect_timeout, s.http_read_timeout)
        self._retry_attempts = s.http_retry_attempts
        self._retry_base_delay = s.http_retry_base_delay

    # ── low-level request ────────────────────────────────────────────────────
    def _auth_header(self) -> dict[str, str]:
        # The two-key scheme. We never log this value.
        return {"Authorization": f"ODFHIR {self._dev_key}/{self._customer_key}"}

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        """Issue a request. Idempotent reads (GET) are retried on transient
        failure (PMSUnavailable); writes are single-shot — a retried POST could
        create a duplicate appointment, so we never auto-retry them."""
        if method.upper() == "GET" and self._retry_attempts > 1:
            return await retry_async(
                lambda: self._request_once(method, path, **kw),
                attempts=self._retry_attempts,
                base_delay=self._retry_base_delay,
                retry_on=PMSUnavailable,
                label=f"Open Dental {method} {path}",
            )
        return await self._request_once(method, path, **kw)

    async def _request_once(self, method: str, path: str, **kw) -> httpx.Response:
        # Defense-in-depth against cross-tenant calls: never hit Open Dental with
        # a missing key. A blank customer key would otherwise mean "use whatever
        # the env default points at" — i.e. potentially ANOTHER clinic's office.
        if not self._dev_key or not self._customer_key:
            raise PMSError("Open Dental keys not configured for this clinic")
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._auth_header(),
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                resp = await client.request(method, path, **kw)
            except httpx.HTTPError as exc:
                logger.warning("Open Dental %s %s transport error: %s", method, path, exc)
                raise PMSUnavailable(str(exc)) from exc
        if resp.status_code >= 500:
            logger.warning("Open Dental %s %s -> %s", method, path, resp.status_code)
            raise PMSUnavailable(f"Open Dental {resp.status_code}")
        if resp.status_code >= 400:
            # 4xx = our request was wrong / no slot / not found — caller decides.
            # SECURITY: do NOT log or surface the response BODY — Open Dental error
            # bodies on patient/appointment endpoints can echo PHI (names, phones).
            # Status + path only; the body never reaches our logs or exceptions.
            logger.info("Open Dental %s %s -> %s", method, path, resp.status_code)
            raise PMSError(f"Open Dental {resp.status_code} on {method} {path}")
        return resp

    # ── Patients ─────────────────────────────────────────────────────────────
    def _parse_patient(self, d: dict) -> PMSPatient:
        return PMSPatient(
            pms_external_id=str(d.get("PatNum", "")),
            first_name=d.get("FName", ""),
            last_name=d.get("LName", ""),
            phone=d.get("WirelessPhone") or d.get("HmPhone") or d.get("Phone", ""),
            email=d.get("Email") or None,
        )

    async def get_patient(self, pms_external_id: str) -> PMSPatient | None:
        try:
            resp = await self._request("GET", f"/patients/{pms_external_id}")
        except PMSError:
            return None
        return self._parse_patient(resp.json())

    async def get_patient_by_phone(self, phone: str) -> PMSPatient | None:
        """The caller's own chart, or None.

        Two things verified against Open Dental's hosted test database, both of
        which the previous version got wrong:

        The parameter is ``Phone``, capitalised. Lowercase ``phone`` is a 400,
        so this lookup failed on every single call — the agent then treated a
        returning patient as brand new and asked them for a date of birth their
        office already has.

        And the search is FUZZY. Sending "5550100199" returned a different
        patient than "+15550100199" did. Taking rows[0] therefore risked
        attaching a caller to a stranger's chart, and everything downstream —
        their appointments, their history — would have been that stranger's. So
        every candidate is checked against the number we actually dialled, and
        nothing but an exact match is accepted.
        """
        resp = await self._request("GET", "/patients", params={"Phone": phone})
        rows = resp.json()
        if not isinstance(rows, list):
            return None
        wanted = _digits(phone)
        exact = [
            row for row in rows
            if any(_digits(row.get(f) or "") == wanted
                   for f in ("WirelessPhone", "HmPhone", "WkPhone"))
        ]
        if len(exact) > 1:
            # A household shares a phone. A parent's mobile sits on the
            # children's charts, and picking one of them would file this visit
            # against a person who did not call. Their own front desk asks who is
            # calling; so do we, by treating the caller as somebody we do not yet
            # recognise rather than as the wrong somebody.
            record_alert("pms_patient_phone_ambiguous", f"matches={len(exact)}")
            return None
        return self._parse_patient(exact[0]) if exact else None

    async def find_appointment_slots(
        self,
        *,
        start_date: str,
        days: int = 1,
        slot_length: int = 60,
        provider_ids: list[str] | None = None,
    ) -> list[PmsSlot]:
        """Open slots in the shape the availability service expects.

        compute_pms_slots calls find_appointment_slots on whatever client the
        bridge hands it. NexHealth and Kolla have one; Open Dental had only
        check_availability, with a different signature and a different return
        type — so the first real call to an Open Dental practice would have
        raised AttributeError inside the availability path, which catches
        NexHealth errors and nothing else, mid-conversation with a patient.

        Never caught live because this adapter had never run against a server.
        """
        end = (
            datetime.strptime(start_date, "%Y-%m-%d").date() + timedelta(days=days)
        ).isoformat()
        slots = await self.check_availability(
            start_date, end,
            length_minutes=slot_length,
            prov_num=(provider_ids[0] if provider_ids else None),
        )
        return [
            PmsSlot(
                start_time=f"{s.date}T{s.time}:00",
                provider_id=s.prov_num or "",
                operatory_id=s.op_num,
            )
            for s in slots
            if s.date and s.time
        ]

    async def create_patient(
        self, first_name: str, last_name: str, phone: str, email: str | None = None
    ) -> PMSPatient:
        body = {"LName": last_name, "FName": first_name, "WirelessPhone": phone}
        if email:
            body["Email"] = email
        resp = await self._request("POST", "/patients", json=body)
        return self._parse_patient(resp.json())

    # ── Reference data ───────────────────────────────────────────────────────
    async def list_providers(self) -> list[PMSProvider]:
        resp = await self._request("GET", "/providers")
        return [
            PMSProvider(
                prov_num=str(p.get("ProvNum", "")),
                name=(f"{p.get('FName', '')} {p.get('LName', '')}".strip()
                      or p.get("Abbr", "Provider")),
                abbreviation=p.get("Abbr"),
            )
            for p in resp.json()
        ]

    async def list_operatories(self) -> list[PMSOperatory]:
        resp = await self._request("GET", "/operatories")
        return [
            PMSOperatory(
                op_num=str(o.get("OperatoryNum", "")),
                name=o.get("OpName", o.get("Abbrev", "Op")),
                prov_num=str(o["ProvDentist"]) if o.get("ProvDentist") else None,
            )
            for o in resp.json()
        ]

    # ── Availability + appointments ──────────────────────────────────────────
    async def check_availability(
        self,
        date_from: str,
        date_to: str,
        preferred_window: str | None = None,
        *,
        length_minutes: int = 60,
        prov_num: str | None = None,
        op_num: str | None = None,
    ) -> list[AvailableSlot]:
        params: dict[str, str] = {
            "dateStart": date_from, "dateEnd": date_to,
            "lengthMinutes": str(length_minutes),
        }
        if prov_num:
            params["ProvNum"] = prov_num
        if op_num:
            params["OpNum"] = op_num
        resp = await self._request("GET", "/appointments/Slots", params=params)
        slots: list[AvailableSlot] = []
        for s in resp.json():
            start = s.get("DateTimeStart") or s.get("AptDateTime", "")
            d, _, t = start.partition(" ")
            slots.append(AvailableSlot(
                date=d, time=t[:5],  # "HH:MM"
                provider=str(s.get("ProvNum", "")),  # name resolved later if needed
                prov_num=str(s.get("ProvNum")) if s.get("ProvNum") is not None else None,
                op_num=str(s.get("OpNum")) if s.get("OpNum") is not None else None,
            ))
        return slots

    def _parse_appointment(self, d: dict) -> CreatedAppointment:
        pattern = d.get("Pattern", "")
        return CreatedAppointment(
            pms_external_id=str(d.get("PatNum", "")),
            apt_num=str(d.get("AptNum", "")) or None,
            appointment_at=d.get("AptDateTime", ""),
            duration_minutes=max(5, len(pattern) * 5) if pattern else 60,
            procedure_type=d.get("ProcDescript", "appointment"),
            provider_name=str(d.get("ProvNum", "")),
        )

    async def create_appointment(
        self,
        pms_external_id: str,
        appointment_at: str,
        procedure_type: str,
        duration_minutes: int = 60,
        *,
        op_num: str | None = None,
        prov_num: str | None = None,
    ) -> CreatedAppointment:
        body: dict = {
            "PatNum": pms_external_id,
            "AptDateTime": _to_od_datetime(appointment_at),
            "Pattern": _pattern_for(duration_minutes),
        }
        if op_num:
            body["Op"] = op_num
        if prov_num:
            body["ProvNum"] = prov_num
        resp = await self._request("POST", "/appointments", json=body)
        return self._parse_appointment(resp.json())

    async def get_appointment(self, apt_num: str) -> CreatedAppointment | None:
        try:
            resp = await self._request("GET", f"/appointments/{apt_num}")
        except PMSError:
            return None
        return self._parse_appointment(resp.json())

    async def reschedule_appointment(
        self,
        apt_num: str,
        new_appointment_at: str,
        *,
        op_num: str | None = None,
        prov_num: str | None = None,
    ) -> CreatedAppointment:
        body: dict = {
            "AptStatus": "Scheduled",
            "AptDateTime": _to_od_datetime(new_appointment_at),
        }
        if op_num:
            body["Op"] = op_num
        if prov_num:
            body["ProvNum"] = prov_num
        resp = await self._request("PUT", f"/appointments/{apt_num}", json=body)
        return self._parse_appointment(resp.json())

    async def cancel_appointment(
        self, apt_num: str, *, send_to_unscheduled: bool = True
    ) -> bool:
        # Open Dental "breaks" an appointment; sendToUnscheduledList keeps it for
        # rebooking instead of deleting it (safer + reversible).
        await self._request(
            "PUT", f"/appointments/{apt_num}/Break",
            json={"sendToUnscheduledList": "true" if send_to_unscheduled else "false"},
        )
        return True
