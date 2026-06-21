"""CORS origin parsing (feat/cors-from-env).

CORS_ALLOWED_ORIGINS is a comma-separated env string of EXACT origins (no
wildcards). `Settings.cors_origins_list` must parse it into a clean list,
trimming whitespace and dropping empty entries.
"""

from __future__ import annotations

from app.config import Settings


def _settings(value: str) -> Settings:
    # Pass explicitly so the test doesn't depend on process env / .env.
    return Settings(cors_allowed_origins=value)


def test_default_is_local_dev_only():
    s = Settings()
    assert s.cors_origins_list == ["http://localhost:3000"]
    # No Vercel wildcard leaks into the default.
    assert not any("*" in o for o in s.cors_origins_list)


def test_single_origin():
    s = _settings("https://dentiva-dashboard.vercel.app")
    assert s.cors_origins_list == ["https://dentiva-dashboard.vercel.app"]


def test_multiple_origins_comma_separated():
    s = _settings("http://localhost:3000,https://app.dentiva.com")
    assert s.cors_origins_list == [
        "http://localhost:3000",
        "https://app.dentiva.com",
    ]


def test_whitespace_and_trailing_comma_are_cleaned():
    s = _settings(" http://localhost:3000 , https://app.dentiva.com , ")
    assert s.cors_origins_list == [
        "http://localhost:3000",
        "https://app.dentiva.com",
    ]


def test_empty_value_yields_empty_list():
    s = _settings("")
    assert s.cors_origins_list == []
