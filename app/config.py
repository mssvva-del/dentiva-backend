"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _to_async_url(url: str) -> str:
    """Normalize ANY Postgres URL to the asyncpg driver.

    The app's SQLAlchemy engine is async and *requires* the asyncpg driver.
    Hosts inject the URL in several shapes, all coerced to
    ``postgresql+asyncpg://``:
      * ``postgres://`` (Heroku/legacy),
      * ``postgresql://`` (Railway/Render plain),
      * ``postgresql+psycopg2://`` / ``postgresql+psycopg://`` (sync driver) —
        if a sync scheme leaks into DATABASE_URL the async engine crashes on
        startup, killing both ``alembic upgrade`` and uvicorn before /health
        is ever reachable.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    # Strip any explicit *sync* driver so the async engine always gets asyncpg.
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql://" + url[len("postgresql+psycopg2://") :]
    elif url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _to_sync_url(url: str) -> str:
    """Normalize ANY Postgres URL to the psycopg2 (sync) driver used by Alembic.

    Mirrors ``_to_async_url`` but targets psycopg2, so it works both for a URL
    derived from ``DATABASE_URL`` (async) and for an explicitly-provided
    ``DATABASE_URL_SYNC`` that may arrive as ``postgres://`` / ``postgresql://``
    / ``postgresql+asyncpg://``.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url[len("postgresql+asyncpg://") :]
    elif url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql+psycopg2://"):
        return url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+asyncpg://dentiva:dentiva@localhost:5432/dentiva"
    database_url_sync: str = "postgresql+psycopg2://dentiva:dentiva@localhost:5432/dentiva"

    @field_validator("*", mode="before")
    @classmethod
    def _strip_whitespace(cls, v):
        """Trim stray whitespace from env values (e.g. a tab pasted before
        ``true``). Prevents a single bad paste from crashing startup — a leading
        tab made Pydantic fail to parse a bool, killing the whole app."""
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _normalize_db_urls(self) -> "Settings":
        # Accept a single plain DATABASE_URL (Railway/Render style) and derive the
        # async driver URL from it.
        self.database_url = _to_async_url(self.database_url)
        # DATABASE_URL_SYNC (Alembic) is normally derived from DATABASE_URL — but
        # when it's set EXPLICITLY, honor it. This lets the app connect as a
        # restricted RLS role (DATABASE_URL=dentiva_app) while migrations still run
        # as the superuser (DATABASE_URL_SYNC=postgresql://dentiva:...). Without
        # this, deriving sync from async would run Alembic as the unprivileged role
        # and break DDL migrations on deploy.
        if "database_url_sync" in self.model_fields_set:
            self.database_url_sync = _to_sync_url(self.database_url_sync)
        else:
            self.database_url_sync = _to_sync_url(self.database_url)
        return self

    # Encryption
    # ENCRYPTION_KEY is the PRIMARY (current) Fernet key — all new ciphertext is
    # written with it. To rotate, generate a new key, move the old one into
    # ENCRYPTION_KEYS_OLD (comma-separated, decrypt-only) and set the new one as
    # ENCRYPTION_KEY. Old data keeps decrypting via the retired keys until it's
    # re-encrypted. Never delete an old key while any row may still hold data
    # encrypted with it.
    encryption_key: str = ""
    encryption_keys_old: str = ""

    # Clerk
    clerk_publishable_key: str = ""
    clerk_secret_key: str = ""
    clerk_jwks_url: str = ""
    # svix signing secret for Clerk webhooks (Dashboard → Webhooks → Signing
    # Secret, format "whsec_..."). Verifies user.created / organization.* events.
    # When empty: allowed in dev (logged), but REJECTED in production so an
    # unverified webhook can't provision/delete users. Set in Railway before go-live.
    clerk_webhook_secret: str = ""

    # Retell
    retell_webhook_secret: str = ""
    retell_api_key: str = ""
    retell_agent_id: str = ""

    # Stripe (Phase D). Test-mode keys first; Sergio provides them later. When
    # stripe_secret_key is empty, billing API calls (checkout) return a clear
    # "not configured" error instead of crashing. The webhook secret gates
    # /webhooks/stripe (rejected in production when unset, like the Clerk one).
    # Price IDs map our plan keys → Stripe Price objects (monthly/annual).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Where Stripe Checkout returns the user (success/cancel). The dashboard
    # origin; defaults to local dev. Set to the deployed dashboard URL in prod.
    dashboard_base_url: str = "http://localhost:3000"
    stripe_price_starter_monthly: str = ""
    stripe_price_starter_annual: str = ""
    stripe_price_practice_monthly: str = ""
    stripe_price_practice_annual: str = ""
    stripe_price_group_monthly: str = ""
    stripe_price_group_annual: str = ""

    # Background call-sync: periodically pull recent Retell calls into the DB so
    # the dashboard stays current even when web/test calls fire no webhook.
    # Disabled by default; enable on the server with CALL_SYNC_ENABLED=true.
    call_sync_enabled: bool = False
    call_sync_interval_seconds: int = 300
    call_sync_limit: int = 20

    # Twilio SMS — booking confirmation texts. Disabled by default; enable on
    # the server with SMS_ENABLED=true once the three Twilio values are set.
    # We call the Twilio REST API directly via httpx (no extra SDK dependency).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    sms_enabled: bool = False
    # Inbound SMS (two-way). Validate Twilio's X-Twilio-Signature only when true —
    # behind a proxy the public URL must be reconstructed exactly, so it's off by
    # default for local/dev and enabled in production once the URL is verified.
    twilio_validate_signature: bool = False

    # Appointment reminders — background loop texts patients ~24h and ~2h before
    # their appointment. Disabled by default; enable with REMINDERS_ENABLED=true.
    # Sending also requires SMS_ENABLED + Twilio config (shared with confirmations).
    reminders_enabled: bool = False
    reminder_interval_seconds: int = 900  # how often the loop checks (15 min)
    # Quiet hours (practice-local): never send reminders before this hour or at/
    # after the end hour. Default 8:00–21:00 local. TCPA-friendly.
    reminder_quiet_start_hour: int = 8  # earliest hour reminders may send
    reminder_quiet_end_hour: int = 21  # first hour reminders stop (exclusive)

    # PMS
    # 'mock' (demo/default — fake calendar) or 'open_dental_real' (live API).
    # Stays 'mock' globally; the real adapter is opted into PER PRACTICE so one
    # pilot clinic can run live while everyone else stays on the mock.
    pms_adapter: str = "mock"
    # Open Dental REST base, e.g. https://api.opendental.com/api/v1 (remote) or a
    # clinic's local/service URL. The DEVELOPER key is ours (one for all of
    # Dentiva); each clinic's CUSTOMER key is per-practice and passed in at call
    # time (stored encrypted in practices.pms_credentials_secret_key) — NOT here.
    open_dental_api_url: str = "https://api.opendental.com/api/v1"
    open_dental_dev_key: str = ""
    # Fallback CUSTOMER key for single-clinic/dev use when not threading per-
    # practice keys yet (Step 3 wires per-practice). Empty in prod.
    open_dental_customer_key: str = ""

    # NexHealth (Synchronizer aggregator — covers Dentrix/Eaglesoft/Open Dental/etc).
    # Primary PMS path for Phase 1. Empty keys → use the in-memory mock source so
    # the Reactivation Engine builds/tests without real access. Real keys go in
    # _KEYS_PRIVATE.md → Railway env (NOT git). Confirm prod vs sandbox on arrival.
    nexhealth_api_url: str = "https://nexhealth.info"
    nexhealth_api_key: str = ""
    nexhealth_subdomain: str = ""      # practice subdomain in NexHealth
    nexhealth_location_id: str = ""    # NexHealth location id

    # LLM (Groq primary, Anthropic fallback — OpenAI unused in weekend mode)
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_model_fast: str = "llama-3.1-8b-instant"
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # CORS — comma-separated list of EXACT allowed origins (no wildcards). The
    # browser sends credentials, so wildcards are unsafe and disallowed by the
    # CORS spec anyway. Defaults to local dev; set CORS_ALLOWED_ORIGINS in prod
    # to the exact dashboard domain(s), e.g. "https://dentiva-dashboard.vercel.app".
    cors_allowed_origins: str = "http://localhost:3000"

    # Misc
    environment: str = "development"
    log_level: str = "INFO"
    # Emit one-JSON-object-per-line logs (request_id + practice_id + PHI scrub).
    # Off by default so local dev keeps human-readable logs; turn on in prod for
    # machine-parseable, correlatable logs (and as the substrate Sentry plugs into).
    json_logs: bool = False

    # Auth bypass — ONLY for local tests/dev when no Clerk instance is reachable.
    # Never enable in staging/production.
    auth_dev_bypass: bool = False

    # Demo open-access: any authenticated Clerk user with no local record is
    # auto-attached to the demo (first) practice as staff. Lets you hand a
    # login to a doctor/investor without per-user provisioning. Demo only —
    # everyone shares the same demo data. Turn off before real multi-tenant use.
    demo_open_access: bool = False

    # Lazy provisioning: when a Clerk-authenticated user has no local record yet,
    # provision them from their VERIFIED JWT org claim — create their practice
    # (status='onboarding') if missing and link them with a sensible role. This is
    # the in-spec "first login without a clinic → onboarding wizard" path and makes
    # the system robust to delayed/failed Clerk webhooks (the webhook stays the
    # primary, idempotent provisioning path). Unlike demo_open_access it is fully
    # multi-tenant (keys off the caller's OWN org, never the first/demo practice).
    # Off by default so production behavior is unchanged unless explicitly enabled.
    lazy_provisioning: bool = False

    # Security / ops hardening (Security Sprint — Block 0)
    enable_llm_relay: bool = False  # mount the Groq /ws/retell-llm relay only if true
    rate_limit_enabled: bool = False  # in-process per-IP rate limiting (enable for real traffic)
    rate_limit_per_minute: int = 240  # general endpoints, per client IP
    rate_limit_webhook_per_minute: int = 600  # webhooks (a single call bursts many)

    # Outbound HTTP resilience (audit Addition 3). Connect fails fast; read gets
    # the longer budget. Retry applies ONLY to idempotent reads (GET) — writes are
    # never auto-retried (a retried POST could double-book).
    http_connect_timeout: float = 5.0
    http_read_timeout: float = 15.0
    http_retry_attempts: int = 2  # total tries for idempotent calls (1 = no retry)
    http_retry_base_delay: float = 0.2  # seconds; exponential backoff base

    # Maintenance: prune the webhook-dedup ledger. The idempotency window only
    # needs to outlast a provider's redelivery horizon (hours), so 90 days is a
    # very safe TTL that keeps the table small. Runs daily; on by default (cheap).
    maintenance_enabled: bool = True
    maintenance_interval_seconds: int = 86_400  # once a day
    processed_event_ttl_days: int = 90

    # Sentry error monitoring. Empty DSN → disabled (dev/tests). PII is sent only
    # via our scrubbed before_send (send_default_pii stays False — HIPAA).
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0  # perf tracing off until we need it

    @model_validator(mode="after")
    def _guard_production(self) -> "Settings":
        """Fail-safe: never let dangerous dev flags run in production."""
        if self.environment == "production" and self.auth_dev_bypass:
            raise ValueError(
                "AUTH_DEV_BYPASS must NOT be enabled when ENVIRONMENT=production."
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ALLOWED_ORIGINS (comma-separated) into a clean list.

        Trims whitespace and drops empty entries so a trailing comma or padded
        env value can't inject a bogus/empty origin.
        """
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
