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

from app.adapters.nexhealth.models import PMSReactivationRecord
from app.adapters.nexhealth.source import ReactivationSource
from app.config import get_settings
from app.utils.resilience import make_timeout, retry_async

logger = logging.getLogger("dentiva.pms.nexhealth")

# NexHealth versioned Accept header (TO VERIFY exact version against sandbox).
_ACCEPT = "application/vnd.Nexhealth+json;version=2"


class NexHealthError(Exception):
    """Non-recoverable NexHealth error (4xx) — caller decides fallback."""


class NexHealthUnavailable(Exception):
    """Transient NexHealth failure (5xx / network / timeout) — retryable."""


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
                    "/authenticates", headers={"Authorization": self._api_key}
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
            headers = {"Authorization": f"Bearer {self._token}", "Accept": _ACCEPT}
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
            last_visit_date=_parse_date(d.get("last_visit_date")),
            recall_due_date=_parse_date(d.get("recall_due_date")),
            contactable=not bool(d.get("unsubscribe_sms")),
        )

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
        return out[:limit]
