"""Real NexHealth reactivation pull client (Phase 1, block 2).

NOT yet wired to a live clinic — built + tested against a MOCKED NexHealth API
(httpx.MockTransport). Selected automatically only when NexHealth keys are set;
until then ``get_reactivation_source`` returns the in-memory mock.

Auth (NexHealth two-step, TO VERIFY against sandbox):
  1. POST /authenticates  header ``Authorization: <api_key>``  → bearer token (~1h)
  2. requests send ``Authorization: Bearer <token>`` + the NexHealth Accept header,
     scoped by ``subdomain`` + ``location_id`` query params.

Endpoint/field shapes marked TO VERIFY are best-guess from public docs and MUST
be confirmed once we have sandbox/prod keys (see _docs/QUESTIONS.md). Every HTTP
call goes through an injectable transport, so the parsing/pagination logic is
fully unit-tested without the network.

Scope (block 2): the reactivation PULL only. Booking write-back
(slots/create/reschedule/cancel) is block 8, gated on real keys + a real number.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import httpx

from app.adapters.nexhealth.models import (
    NexHealthAppointment,
    NexHealthSlot,
    PMSReactivationRecord,
)
from app.adapters.nexhealth.source import ReactivationSource
from app.config import get_settings
from app.observability.alerts import record_alert
from app.utils.resilience import make_timeout, retry_async

logger = logging.getLogger("dentiva.pms.nexhealth")

# NexHealth versioned Accept header (CONFIRMED against sandbox 2026-06-24).
_ACCEPT = "application/vnd.Nexhealth+json;version=2"
# WHY a real User-Agent is REQUIRED: NexHealth sits behind Cloudflare, which
# blocks the default httpx UA (error 1010 → 403). Confirmed against the sandbox —
# without this every live request fails. Identify ourselves explicitly.
_UA = "Dentovox/1.0 (+https://dentovox.com)"

# The API version we are written against. Sending nothing does NOT mean "latest"
# — NexHealth falls back to the legacy 2.2 API, which is where /appointment_slots
# and /availabilities live. We were calling both, getting empty results, and
# reading that as "this clinic has no schedule". Their support named it: those
# endpoints "can only be used with the legacy, deprecated version of our API".
# v3.0.0 calls them /available_slots and /working_hours.
_API_VERSION = "v3.0.0"

# Base headers on every authenticated call.
_BASE_HEADERS = {"Accept": _ACCEPT, "User-Agent": _UA, "Nex-Api-Version": _API_VERSION}


def _money_to_cents(value: object) -> int:
    """Dollar amount (string/number) → int cents. Dirty/blank → 0."""
    if value in (None, "", False):
        return 0
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return 0


def _balance_to_cents(balance: object) -> int:
    """NexHealth patient 'balance' → int cents. CONFIRMED against sandbox: it's an
    object ``{"amount": "12.50", "currency": "USD"}`` — NOT a flat string. We still
    accept a flat value for forward/back compatibility and mocked tests."""
    if isinstance(balance, dict):
        return _money_to_cents(balance.get("amount"))
    return _money_to_cents(balance)


# Paths whose request carries no patient data, so a 4xx body can be quoted back
# into an alert. Everything else stays status-and-path only.
_BODY_SAFE_PATHS = frozenset({
    "/providers", "/locations", "/available_slots", "/working_hours",
    "/appointment_types", "/institutions",
})


# Writes whose 4xx body is about an APPOINTMENT — its state, its time — and not
# about the person. /patients is deliberately absent: its error text echoes the
# name, number and date of birth we just sent, which a test caught leaking into
# an exception message the moment this was written too broadly.
_REFUSAL_SAFE_PREFIX = "/appointments"


def _refusal(resp: httpx.Response, path: str) -> str:
    """Only NexHealth's own reason for refusing an APPOINTMENT write, trimmed.

    A cancellation that their API rejects currently reaches us as "pms_error"
    and nothing else — three of them sat in a live clinic's calendar while the
    log said only that something had gone wrong. Their refusals read
    {"error": ["Cannot update already cancelled appointment"]}: a sentence about
    the appointment's state, not about the patient.

    Narrower than _why on purpose, twice over: only the "error" field (a
    "description" echoes the parameters we sent), and only on appointment paths.
    """
    if not path.startswith(_REFUSAL_SAFE_PREFIX):
        return ""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON error page is not a crash
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, list):
        err = "; ".join(str(e) for e in err if e)
    return f" — {str(err)[:200]}" if err else ""


def _why(resp: httpx.Response) -> str:
    """NexHealth's own explanation of a 4xx, trimmed. Never raises."""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON error page is not a crash
        return resp.text[:120]
    for key in ("description", "error", "message"):
        val = payload.get(key) if isinstance(payload, dict) else None
        if val:
            return str(val)[:200]
    return str(payload)[:200]


class NexHealthError(Exception):
    """Non-recoverable NexHealth error (4xx) — caller decides fallback."""


class NexHealthUnavailable(Exception):
    """Transient NexHealth failure (5xx / network / timeout) — retryable."""


class NexHealthDuplicate(NexHealthError):
    """The patient we tried to create is already in the practice's system.

    Carries their id, because this is not really a failure — the person exists,
    which is what we wanted — and losing a booking over it would be absurd.
    """

    def __init__(self, patient_id: str) -> None:
        super().__init__("patient already exists")
        self.patient_id = patient_id


# The only part of a NexHealth error body we ever keep. Error text can echo what
# we sent, which for this endpoint is a real person's name and number, so the
# body is matched and discarded rather than stored or logged.
_DUPLICATE_ID_RE = re.compile(r"already exists\D+id=(\d+)")


def _str_or_none(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_date(value: object) -> date | None:
    """Lenient ISO-date parse — bad/missing dates become None, never raise.
    Real PMS data is dirty; one malformed date must not kill the whole pull."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class NexHealthClient(ReactivationSource):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        subdomain: str | None = None,
        location_id: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> None:
        s = get_settings()
        self._api_key = api_key or s.nexhealth_api_key
        self._subdomain = subdomain or s.nexhealth_subdomain
        self._location_id = location_id or s.nexhealth_location_id
        self._base_url = (base_url or s.nexhealth_api_url).rstrip("/")
        self._transport = transport
        self._timeout = timeout or make_timeout(s.http_connect_timeout, s.http_read_timeout)
        self._retry_attempts = s.http_retry_attempts
        self._retry_base_delay = s.http_retry_base_delay
        self._token: str | None = None  # cached bearer token (fetched lazily)
        # Provider roster for this location, resolved once per client.
        self._provider_ids: list[str] | None = None

    # ── auth ──────────────────────────────────────────────────────────────────
    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        )

    async def _fetch_token(self) -> str:
        """Exchange the long-lived api_key for a short-lived bearer token."""
        async with await self._client() as client:
            try:
                resp = await client.post(
                    "/authenticates",
                    headers={"Authorization": self._api_key, **_BASE_HEADERS},
                )
            except httpx.HTTPError as exc:
                raise NexHealthUnavailable(f"auth transport error: {exc}") from exc
        if resp.status_code >= 500:
            raise NexHealthUnavailable(f"auth {resp.status_code}")
        if resp.status_code >= 400:
            # Never log the body — auth errors can echo the key.
            raise NexHealthError(f"auth failed {resp.status_code}")
        token = (resp.json().get("data") or {}).get("token")
        if not token:
            raise NexHealthError("auth response missing token")
        return token

    async def _resolve_subdomain(self) -> str:
        """The institution this API key belongs to.

        Every other endpoint is scoped by ``subdomain``, and NexHealth rejects a
        blank one outright: "Blank values not allowed for parameter subdomain".
        A clinic connected without it looked fully connected on every screen we
        have, while its calendar answered 400 to every question — the agent fell
        back to our own book and no booking ever reached the practice.

        /institutions is the one endpoint that does not need it. One institution
        is the normal case for a clinic's own key. TWO is refused rather than
        guessed: picking the first would read and write a different practice's
        calendar, which is not a mistake you can take back.
        """
        if self._subdomain:
            return self._subdomain
        if self._token is None:
            self._token = await self._fetch_token()
        headers = {"Authorization": f"Bearer {self._token}", **_BASE_HEADERS}
        async with await self._client() as client:
            try:
                resp = await client.get("/institutions", headers=headers)
            except httpx.HTTPError as exc:
                raise NexHealthUnavailable(str(exc)) from exc
        if resp.status_code >= 500:
            raise NexHealthUnavailable(f"NexHealth {resp.status_code} on GET /institutions")
        if resp.status_code >= 400:
            raise NexHealthError(
                f"NexHealth {resp.status_code} on GET /institutions — {_why(resp)}"
            )
        rows = (resp.json() or {}).get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("institutions") or []
        subs = [
            str(r["subdomain"]) for r in rows
            if isinstance(r, dict) and r.get("subdomain")
        ]
        if len(subs) != 1:
            record_alert("pms_subdomain_ambiguous", f"institutions={len(subs)}")
            raise NexHealthError(
                f"cannot resolve subdomain: {len(subs)} institutions on this key"
            )
        self._subdomain = subs[0]
        # Named so it can be written into the practice's credentials and stop
        # costing a request per client. An institution subdomain is not PHI.
        record_alert("pms_subdomain_resolved", f"subdomain={subs[0]}")
        return self._subdomain

    async def _get(self, path: str, params: dict) -> httpx.Response:
        """Authenticated GET with one token-refresh retry on 401, plus the shared
        transient retry (idempotent read)."""

        async def _once() -> httpx.Response:
            if self._token is None:
                self._token = await self._fetch_token()
            subdomain = self._subdomain or await self._resolve_subdomain()
            scoped = {"subdomain": subdomain, "location_id": self._location_id, **params}
            headers = {"Authorization": f"Bearer {self._token}", **_BASE_HEADERS}
            async with await self._client() as client:
                try:
                    resp = await client.get(path, params=scoped, headers=headers)
                except httpx.HTTPError as exc:
                    raise NexHealthUnavailable(str(exc)) from exc
            if resp.status_code == 401:
                # Token expired → drop it so the next attempt re-auths.
                self._token = None
                raise NexHealthUnavailable("401 — token refresh needed")
            if resp.status_code >= 500:
                raise NexHealthUnavailable(f"NexHealth {resp.status_code}")
            if resp.status_code >= 400:
                # Status + path is enough to know something is wrong and not
                # enough to fix it: two different 400s on /providers and
                # /appointment_slots cost a deploy cycle each to tell apart.
                #
                # NexHealth answers a 4xx with {"description": "..."} naming the
                # parameter it objected to. That is safe to keep ONLY where the
                # request carried no patient data — a 400 from /patients could
                # echo the phone number we searched on. Hence the allow-list:
                # these three are asked with a subdomain, a location and dates.
                raise NexHealthError(
                    f"NexHealth {resp.status_code} on GET {path}"
                    + (f" — {_why(resp)}" if path in _BODY_SAFE_PATHS else "")
                )
            return resp

        if self._retry_attempts > 1:
            return await retry_async(
                _once, attempts=self._retry_attempts, base_delay=self._retry_base_delay,
                retry_on=NexHealthUnavailable, label=f"NexHealth GET {path}",
            )
        return await _once()

    async def _patch(self, path: str, body: dict) -> httpx.Response:
        """Authenticated PATCH. Single-shot for the same reason as _post: a
        retried write is a write we cannot prove happened only once."""
        if self._token is None:
            self._token = await self._fetch_token()
        scoped = {"subdomain": self._subdomain, "location_id": self._location_id}
        headers = {"Authorization": f"Bearer {self._token}", **_BASE_HEADERS}
        async with await self._client() as client:
            try:
                resp = await client.patch(path, params=scoped, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise NexHealthUnavailable(str(exc)) from exc
        if resp.status_code in (401, 500, 502, 503, 504):
            self._token = None if resp.status_code == 401 else self._token
            raise NexHealthUnavailable(f"NexHealth {resp.status_code} on PATCH {path}")
        if resp.status_code >= 400:
            raise NexHealthError(
                f"NexHealth {resp.status_code} on PATCH {path}" + _refusal(resp, path)
            )
        return resp

    async def _post(self, path: str, body: dict) -> httpx.Response:
        """Authenticated POST. SINGLE-SHOT — never auto-retried: a retried create
        could double-book the patient (the caller decides any fallback)."""
        if self._token is None:
            self._token = await self._fetch_token()
        scoped = {"subdomain": self._subdomain, "location_id": self._location_id}
        headers = {"Authorization": f"Bearer {self._token}", **_BASE_HEADERS}
        async with await self._client() as client:
            try:
                resp = await client.post(path, params=scoped, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise NexHealthUnavailable(str(exc)) from exc
        if resp.status_code in (401, 500, 502, 503, 504):
            # Auth-expiry or server error → transient. We do NOT auto-retry a write;
            # the caller falls back (graceful degradation) rather than risk a double.
            self._token = None if resp.status_code == 401 else self._token
            raise NexHealthUnavailable(f"NexHealth {resp.status_code} on POST {path}")
        if resp.status_code >= 400:
            # One thing is worth reading out of an error body: "this person is
            # already here, and here is their id". Everything else about the body
            # is discarded — error text echoes what we sent, and for /patients
            # that is a real name and number.
            duplicate = _DUPLICATE_ID_RE.search(resp.text or "")
            if duplicate:
                raise NexHealthDuplicate(duplicate.group(1))
            raise NexHealthError(
                f"NexHealth {resp.status_code} on POST {path}" + _refusal(resp, path)
            )
        return resp

    # ── booking write-back (block 8) ──────────────────────────────────────────
    @staticmethod
    def _slots_from(payload: dict) -> list[NexHealthSlot]:
        """Flatten NexHealth's appointment_slots response into flat slots.
        Shape (TO VERIFY against sandbox): data may be a list of provider buckets
        ``[{pid, lid, slots:[{time, operatory_id}]}]`` or already-flat slots."""
        data = payload.get("data") or []
        out: list[NexHealthSlot] = []
        for bucket in data if isinstance(data, list) else []:
            if not isinstance(bucket, dict):
                continue
            pid = str(bucket.get("pid") or bucket.get("provider_id") or "")
            nested = bucket.get("slots")
            if isinstance(nested, list):
                for s in nested:
                    out.append(NexHealthSlot(
                        start_time=str(s.get("time") or s.get("start_time") or ""),
                        provider_id=pid,
                        operatory_id=_str_or_none(s.get("operatory_id")),
                    ))
            elif bucket.get("time") or bucket.get("start_time"):
                out.append(NexHealthSlot(
                    start_time=str(bucket.get("time") or bucket.get("start_time")),
                    provider_id=pid,
                    operatory_id=_str_or_none(bucket.get("operatory_id")),
                ))
        return out

    async def find_appointment_slots(
        self,
        *,
        start_date: str,
        days: int = 1,
        provider_ids: list[str] | None = None,
        slot_length: int = 60,
    ) -> list[NexHealthSlot]:
        """Open slots from start_date over N days (used for the anti-double-book
        re-check before writing an appointment back).

        v3.0.0 calls this ``/available_slots``. The old ``/appointment_slots`` is
        the legacy 2.2 endpoint and answers an empty list rather than an error,
        which reads exactly like a clinic with no free time.

        ``pids[]`` is REQUIRED, not optional. Without it NexHealth answers 400
        and every caller fell back to our own calendar — so a clinic whose PMS
        was connected and healthy was still offered times from our book, and no
        booking ever reached its real calendar. It is filled in from the
        location's own providers when the caller does not name one.

        Requires ``lids[]`` (location), ``pids[]`` (providers) and
        ``slot_length`` in addition to ``start_date``/``days``; returns
        ``data: [{lid, pid, slots: [{time, end_time, operatory_id}]}]`` — exactly
        what _slots_from parses."""
        pids = provider_ids or await self.provider_ids()
        if not pids:
            # No provider, no question worth asking. Returning [] rather than
            # raising keeps this "the clinic has nobody bookable", which the
            # caller handles by falling back to our own book.
            return []
        params: dict = {
            "start_date": start_date,
            "days": days,
            "lids[]": self._location_id,
            "pids[]": pids,
            "slot_length": slot_length,
        }
        return self._slots_from((await self._get("/available_slots", params)).json())

    async def availability_count(self) -> int | None:
        """How many bookable windows this location has defined in NexHealth.

        NexHealth builds open slots from *working hours*, not from the practice
        software's schedule. (Called "availabilities" in the legacy 2.2 API,
        which is what we were asking, and which answers empty rather than
        erroring.) A clinic can be fully synced — patients, providers,
        appointments all flowing — and still answer "no openings" for every day
        of the year because nobody defined when its chairs are bookable. From
        the caller's side that is indistinguishable from a genuinely full book,
        so it has to be asked about explicitly.

        None means we could not ask; 0 means nobody can ever be booked.
        """
        try:
            payload = (await self._get("/working_hours", {"per_page": 100})).json()
        except (NexHealthError, NexHealthUnavailable):
            return None
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("working_hours") or rows.get("availabilities") or []
        return len(rows)

    async def create_appointment(
        self,
        *,
        patient_pms_id: str,
        provider_id: str,
        start_time: str,
        operatory_id: str | None = None,
        note: str | None = None,
        end_time: str | None = None,  # noqa: ARG002 — see below
    ) -> NexHealthAppointment:
        # end_time is accepted and ignored: NexHealth derives the end from the
        # appointment type, while Kolla requires it explicitly. The two clients
        # take the same call so the write-back path never has to ask which PMS
        # bridge it is holding — the moment it asks, it starts getting it wrong.
        """Write an appointment back to the PMS. Returns the PMS appointment id.

        CONFIRMED against sandbox 2026-06-26 (status 201): body is
        ``{"appt": {patient_id, provider_id, operatory_id, start_time, note?}}``.
        Do NOT send ``appointment_type_id`` — the sandbox rejects it with
        "appointment_type_id was not found to be configured for the requested slot"
        unless it matches the operatory's configured types. The response ``data`` is
        the appointment object directly (id, start_time as ISO …Z, end_time, …)."""
        appt: dict = {
            "patient_id": patient_pms_id,
            "provider_id": provider_id,
            "start_time": start_time,
        }
        if operatory_id:
            appt["operatory_id"] = operatory_id
        if note:
            appt["note"] = note
        data = (await self._post("/appointments", {"appt": appt})).json().get("data") or {}
        # data is the appt object directly; tolerate a {"appt": {...}} wrapper too.
        created = data.get("appt") if isinstance(data.get("appt"), dict) else data
        appt_id = created.get("id") if isinstance(created, dict) else None
        if appt_id is None:
            raise NexHealthError("create-appointment response missing id")
        return NexHealthAppointment(
            appointment_id=str(appt_id),
            start_time=str(created.get("start_time") or start_time),
        )

    # ── the patient behind the appointment ───────────────────────────────────

    async def find_patient_id(self, *, phone: str) -> str | None:
        """The PMS id of a patient already in the practice's system, or None.

        This is what keeps most calls short. A returning caller is already in the
        clinic's system, so we adopt their id and ask them nothing; only a
        genuinely new person has to answer for a date of birth.

        The parameter is ``phone_number``, verified against the sandbox
        2026-08-13. ``search`` is silently IGNORED — it answered 200 with the
        first five patients in the practice, none of them the one being looked
        for. A lookup that returns strangers rather than nothing is the worse
        kind of wrong, which is why the digit comparison below is not optional
        even now that the parameter is right.

        Matching is on digits alone: the PMS stores whatever the front desk
        typed years ago, which is "(620) 555-1111" as often as "+16205551111".
        """
        wanted = re.sub(r"\D", "", phone or "")[-10:]
        if len(wanted) < 10:
            return None
        try:
            payload = (await self._get(
                "/patients", {"per_page": 25, "phone_number": wanted}
            )).json()
        except (NexHealthError, NexHealthUnavailable):
            # Not being able to look someone up is not a reason to fail a
            # booking. The caller falls through to creating them, and NexHealth
            # refuses a duplicate on its own.
            return None
        for row in self._patients_from(payload):
            stored = re.sub(r"\D", "", str((row.get("bio") or {}).get("phone_number") or ""))
            if stored[-10:] == wanted and row.get("id") is not None:
                return str(row["id"])
        return None

    async def list_locations(self) -> list[dict]:
        """Every practice connected to OUR NexHealth account: [{id, name}].

        One account key covers all of them, so linking a clinic to its calendar
        is a matter of picking an id — nobody types a credential, and no clinic
        is ever shown another clinic's practice, because only an admin sees this.

        The endpoint answers 200 with an empty list until NexHealth connects a
        practice on their side (their staff install the Synchronizer at the
        clinic). Empty is therefore a normal, expected answer and not a fault.

        Field names beyond id/name are UNVERIFIED: the production account had
        nothing connected when this was written, so there was no row to read.
        Anything missing degrades to a blank label, never to a crash.
        """
        try:
            payload = (await self._get("/locations", {"per_page": 100})).json()
        except (NexHealthError, NexHealthUnavailable):
            return []
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("locations") or []
        out = []
        for row in rows:
            if isinstance(row, dict) and row.get("id") is not None:
                out.append({
                    "id": str(row["id"]),
                    "name": str(row.get("name") or row.get("display_name") or ""),
                })
        return out

    async def provider_ids(self) -> list[str]:
        """Every bookable provider at this location.

        Cached for the life of the client: one phone call asks for availability
        several times as the caller changes their mind, and the roster does not
        change between those questions.
        """
        if self._provider_ids is not None:
            return self._provider_ids
        try:
            payload = (await self._get("/providers", {"per_page": 100})).json()
        except (NexHealthError, NexHealthUnavailable) as exc:
            record_alert("pms_providers_error", f"{type(exc).__name__}: {str(exc)[:120]}")
            return []
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("providers") or []
        ids = [
            str(row["id"])
            for row in rows
            if isinstance(row, dict) and row.get("id") is not None and not row.get("inactive")
        ]
        if not ids:
            record_alert("pms_providers_empty", f"rows={len(rows)}")
        self._provider_ids = ids
        return ids

    async def first_provider_id(self) -> str | None:
        """Any provider at this location — a patient cannot be created without one.

        Which provider hardly matters: NexHealth attaches a new patient to one as
        an administrative owner, and the appointment carries the provider the
        caller was actually offered. Taking the first keeps a phone call from
        having to ask "and which dentist would you like on your file?".
        """
        ids = await self.provider_ids()
        return ids[0] if ids else None

    async def create_patient(
        self,
        *,
        first_name: str,
        last_name: str,
        phone: str,
        date_of_birth: str,
        provider_id: str,
        email: str,
    ) -> str:
        """Create the patient in the practice's own system. Returns their PMS id.

        VERIFIED against the sandbox 2026-08-13, and every part of the shape was
        wrong in the docs we would otherwise have followed:

          * ``provider`` goes in the BODY, not the query string — the documented
            ``?provider_id=`` form answers 400 "Missing parameter provider".
          * ``email`` and ``bio.date_of_birth`` are REQUIRED, not optional. That
            is why the agent has to ask for a date of birth: a call knows a name
            and a number, and an invented birth date lands in the field practices
            match and de-duplicate people on.
          * a duplicate answers 400 "A patient with that information already
            exists - id=NNN". That is not a failure — the person is there, which
            is what we wanted — so the id is read back out of the message rather
            than the booking being lost over it.
        """
        digits = re.sub(r"\D", "", phone or "")[-10:]
        body = {
            "provider": {"provider_id": provider_id},
            "patient": {
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "bio": {"phone_number": digits, "date_of_birth": date_of_birth},
            },
        }
        try:
            payload = (await self._post("/patients", body)).json()
        except NexHealthDuplicate as exc:
            # Already there. The booking proceeds against the person NexHealth
            # just told us about, rather than failing over a success.
            return exc.patient_id
        data = payload.get("data")
        # Their docs wrap the new patient in "user"; the sandbox has returned it
        # bare. Reading the wrapper as the patient would store an id that
        # addresses nothing, and the appointment write would fail later, on a
        # call, rather than here.
        created = data.get("user") if isinstance(data, dict) and "user" in data else data
        patient_id = created.get("id") if isinstance(created, dict) else None
        if patient_id is None:
            raise NexHealthError("create-patient response missing id")
        return str(patient_id)

    # ── pull ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _patients_from(payload: dict) -> list[dict]:
        """Extract the patient list from NexHealth's envelope, tolerant of shape
        (data.patients | data as list). TO VERIFY exact shape against sandbox."""
        data = payload.get("data")
        if isinstance(data, dict):
            rows = data.get("patients")
            return rows if isinstance(rows, list) else []
        return data if isinstance(data, list) else []

    def _parse_patient(self, d: dict) -> PMSReactivationRecord | None:
        """Map one NexHealth patient → DTO. Returns None for an unusable record
        (no id) so the pull skips it instead of crashing. Field paths TO VERIFY."""
        ext = d.get("id")
        if ext is None:
            return None
        bio = d.get("bio") or {}
        # last_visit / recall / treatment value / balance are NOT on the patient
        # object — they come from appointments/recalls/balances endpoints. TO
        # VERIFY + enrich when real keys land; until then they default (mock fills
        # them so the engine has realistic data to build against).
        return PMSReactivationRecord(
            pms_external_id=str(ext),
            first_name=d.get("first_name") or "",
            last_name=d.get("last_name") or "",
            phone=bio.get("phone_number") or d.get("phone_number") or "",
            email=d.get("email") or None,
            preferred_language=(d.get("preferred_language") or "en").lower()[:2],
            # last_visit / recall_due are NOT on the patient object (confirmed
            # against sandbox 2026-06-26) — last_visit is enriched from the
            # /appointments endpoint (see _last_visit_map). recall_due has no
            # sandbox source (/recalls → 404) so it stays None until a PMS exposes it.
            last_visit_date=_parse_date(d.get("last_visit_date")),
            recall_due_date=_parse_date(d.get("recall_due_date")),
            # balance is an OBJECT {amount, currency} on the patient (CONFIRMED
            # sandbox 2026-06-26) — _balance_to_cents reads .amount.
            balance_cents=_balance_to_cents(d.get("balance")),
            # Not contactable if the patient unsubscribed from SMS or is inactive.
            contactable=not (bool(d.get("unsubscribe_sms")) or bool(d.get("inactive"))),
        )

    async def _last_visit_map(self, *, today: date) -> dict[str, date]:
        """Build {patient_id → most-recent past visit date} from /appointments.

        last_visit is NOT on the patient object; the segmentation "lapsed" signal
        comes from the patient's newest completed (not cancelled/unavailable/deleted)
        appointment that started on or before today. One paginated sweep, then a
        dict lookup — cheaper than per-patient calls."""
        out: dict[str, date] = {}
        page, per_page = 1, 100
        while True:
            params = {"page": page, "per_page": per_page,
                      "start": "2000-01-01", "end": today.isoformat()}
            rows = (await self._get("/appointments", params)).json().get("data") or []
            if not isinstance(rows, list) or not rows:
                break
            for a in rows:
                if not isinstance(a, dict):
                    continue
                if a.get("cancelled") or a.get("unavailable") or a.get("deleted"):
                    continue
                pid = a.get("patient_id")
                d = _parse_date(a.get("start_time"))
                if pid is None or d is None or d > today:
                    continue
                key = str(pid)
                if key not in out or d > out[key]:
                    out[key] = d
            if len(rows) < per_page:
                break
            page += 1
        return out

    async def pull_reactivation_records(
        self, *, updated_since: date | None = None, limit: int = 1000
    ) -> list[PMSReactivationRecord]:
        if not (self._api_key and self._subdomain and self._location_id):
            raise NexHealthError("NexHealth keys not configured")
        out: list[PMSReactivationRecord] = []
        page, per_page = 1, 100
        while len(out) < limit:
            params: dict = {"page": page, "per_page": per_page}
            if updated_since is not None:
                params["updated_since"] = updated_since.isoformat()  # TO VERIFY param name
            rows = self._patients_from((await self._get("/patients", params)).json())
            if not rows:
                break
            for d in rows:
                rec = self._parse_patient(d)
                if rec is not None:
                    out.append(rec)
            if len(rows) < per_page:
                break  # last page
            page += 1
        out = out[:limit]

        # Best-effort enrichment: fill last_visit_date from /appointments. A failure
        # here must NEVER drop the pull — segmentation just falls back to "no last
        # visit known" for those patients.
        try:
            visits = await self._last_visit_map(today=date.today())
            for rec in out:
                if rec.last_visit_date is None:
                    rec.last_visit_date = visits.get(rec.pms_external_id)
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            logger.warning("last_visit enrichment skipped: %s", type(exc).__name__)
        return out

    async def cancel_appointment(
        self, appointment_id: str, *, canceler: str | None = None  # noqa: ARG002
    ) -> None:
        """Free the chair in the clinic's calendar.

        UNVERIFIED against the sandbox: our NexHealth key does not authenticate
        yet, so this shape comes from their reference rather than a response we
        have seen. The Kolla path is the one our first customer uses; this exists
        so a NexHealth practice is not silently worse off, and it is marked
        because a shape nobody has run is a guess with good manners.

        ``canceler`` is accepted and ignored — Kolla records who cancelled, and
        the two clients take the same call so the caller never branches on which
        bridge it holds.
        """
        await self._patch(
            f"/appointments/{appointment_id}", {"appt": {"cancelled": True}}
        )

    async def move_appointment(
        self, appointment_id: str, *, start_time: str, end_time: str | None = None  # noqa: ARG002
    ) -> None:
        """Move it rather than cancelling and rebooking, so the appointment keeps
        its history and a failure cannot leave the patient with nothing.

        UNVERIFIED — see cancel_appointment. ``end_time`` is ignored: NexHealth
        derives the end from the appointment type.
        """
        await self._patch(
            f"/appointments/{appointment_id}", {"appt": {"start_time": start_time}}
        )
