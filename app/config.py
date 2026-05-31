"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _to_async_url(url: str) -> str:
    """Normalize any Postgres URL to the asyncpg driver.

    Hosts like Railway/Render inject a plain ``postgres://`` or
    ``postgresql://`` URL; SQLAlchemy + asyncpg need an explicit driver.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
