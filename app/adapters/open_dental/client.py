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
from datetime import datetime

import httpx

from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.models import (
    AvailableSlot,
    CreatedAppointment,
    PMSOperatory,
    PMSPatient,
    PMSProvider,
)
from app.config import get_settings

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


class OpenDentalClient(PMSAdapter):
    def __init__(
        self,
        *,
        developer_key: str | None = None,
        customer_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        s = get_settings()
        self._dev_key = developer_key or s.open_dental_dev_key
        self._customer_key = customer_key or s.open_dental_customer_key
        self._base_url = (base_url or s.open_dental_api_url).rstrip("/")
        # Injectable transport so tests stub responses with httpx.MockTransport.
        self._transport = transport
        self._timeout = timeout

    # ── low-level request ────────────────────────────────────────────────────
    def _auth_header(self) -> dict[str, str]:
        # The two-key scheme. We never log this value.
        return {"Authorization": f"ODFHIR {self._dev_key}/{self._customer_key}"}

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
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
        # TO VERIFY: exact phone query param against a sandbox. Open Dental search
        # is by fields like Phone/WirelessPhone; we send a 'phone' param for now.
        resp = await self._request("GET", "/patients", params={"phone": phone})
        rows = resp.json()
        if isinstance(rows, list) and rows:
            return self._parse_patient(rows[0])
        return None

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
