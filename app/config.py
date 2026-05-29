"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+asyncpg://dentiva:dentiva@localhost:5432/dentiva"
    database_url_sync: str = "postgresql+psycopg2://dentiva:dentiva@localhost:5432/dentiva"

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
