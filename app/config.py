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


def _to_sync_url(async_url: str) -> str:
    """Derive the psycopg2 (sync) URL used by Alembic from the async URL."""
    return async_url.replace("+asyncpg://", "+psycopg2://")


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
        # Accept a single plain DATABASE_URL (Railway/Render style) and derive
        # both the async and the sync driver URLs from it.
        self.database_url = _to_async_url(self.database_url)
        self.database_url_sync = _to_sync_url(self.database_url)
        return self

    # Encryption
    encryption_key: str = ""

    # Clerk
    clerk_publishable_key: str = ""
    clerk_secret_key: str = ""
    clerk_jwks_url: str = ""

    # Retell
    retell_webhook_secret: str = ""
    retell_api_key: str = ""
    retell_agent_id: str = ""

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
    pms_adapter: str = "mock"
    open_dental_api_url: str = ""
    open_dental_dev_key: str = ""

    # LLM (Groq primary, Anthropic fallback — OpenAI unused in weekend mode)
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_model_fast: str = "llama-3.1-8b-instant"
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Misc
    environment: str = "development"
    log_level: str = "INFO"

    # Auth bypass — ONLY for local tests/dev when no Clerk instance is reachable.
    # Never enable in staging/production.
    auth_dev_bypass: bool = False

    # Demo open-access: any authenticated Clerk user with no local record is
    # auto-attached to the demo (first) practice as staff. Lets you hand a
    # login to a doctor/investor without per-user provisioning. Demo only —
    # everyone shares the same demo data. Turn off before real multi-tenant use.
    demo_open_access: bool = False

    # Security / ops hardening (Security Sprint — Block 0)
    enable_llm_relay: bool = False  # mount the Groq /ws/retell-llm relay only if true
    rate_limit_enabled: bool = False  # in-process per-IP rate limiting (enable for real traffic)
    rate_limit_per_minute: int = 240  # general endpoints, per client IP
    rate_limit_webhook_per_minute: int = 600  # webhooks (a single call bursts many)

    @model_validator(mode="after")
    def _guard_production(self) -> "Settings":
        """Fail-safe: never let dangerous dev flags run in production."""
        if self.environment == "production" and self.auth_dev_bypass:
            raise ValueError(
                "AUTH_DEV_BYPASS must NOT be enabled when ENVIRONMENT=production."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
