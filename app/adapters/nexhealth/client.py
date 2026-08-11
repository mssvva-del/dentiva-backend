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
from datetime import date

import httpx

from app.adapters.nexhealth.models import (
    NexHealthAppointment,
    NexHealthSlot,
    PMSReactivationRecord,
)
from app.adapters.nexhealth.source import ReactivationSource
from app.config import get_settings
from app.utils.resilience import make_timeout, retry_async

logger = logging.getLogger("dentiva.pms.nexhealth")

# NexHealth versioned Accept header (CONFIRMED against sandbox 2026-06-24).
_ACCEPT = "application/vnd.Nexhealth+json;version=2"
# WHY a real User-Agent is REQUIRED: NexHealth sits behind Cloudflare, which
# blocks the default httpx UA (error 1010 → 403). Confirmed against the sandbox —
# without this every live request fails. Identify ourselves explicitly.
_UA = "Dentovox/1.0 (+https://dentovox.com)"

# Base headers on every authenticated call.
_BASE_HEADERS = {"Accept": _ACCEPT, "User-Agent": _UA}


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


class NexHealthError(Exception):
    """Non-recoverable NexHealth error (4xx) — caller decides fallback."""


class NexHealthUnavailable(Exception):
    """Transient NexHealth failure (5xx / network / timeout) — retryable."""


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

    async def _get(self, path: str, params: dict) -> httpx.Response:
        """Authenticated GET with one token-refresh retry on 401, plus the shared
        transient retry (idempotent read)."""

        async def _once() -> httpx.Response:
            if self._token is None:
                self._token = await self._fetch_token()
            scoped = {"subdomain": self._subdomain, "location_id": self._location_id, **params}
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
                # PHI-safe: status + path only, never the body.
                raise NexHealthError(f"NexHealth {resp.status_code} on GET {path}")
            return resp

        if self._retry_attempts > 1:
            return await retry_async(
                _once, attempts=self._retry_attempts, base_delay=self._retry_base_delay,
                retry_on=NexHealthUnavailable, label=f"NexHealth GET {path}",
            )
        return await _once()

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
            raise NexHealthError(f"NexHealth {resp.status_code} on POST {path}")
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

        CONFIRMED against sandbox 2026-06-26: requires ``lids[]`` (location) +
        ``slot_length`` in addition to ``start_date``/``days``; returns
        ``data: [{lid, pid, slots: [{time, end_time, operatory_id}]}]`` — exactly
        what _slots_from parses."""
        params: dict = {
            "start_date": start_date,
            "days": days,
            "lids[]": self._location_id,
            "slot_length": slot_length,
        }
        if provider_ids:
            params["pids[]"] = provider_ids
        return self._slots_from((await self._get("/appointment_slots", params)).json())

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
