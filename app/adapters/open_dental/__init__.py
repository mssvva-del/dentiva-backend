"""PMS adapter package. Factory selects mock vs real based on settings."""

from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.mock import MockOpenDentalAdapter
from app.config import get_settings


def get_pms_adapter() -> PMSAdapter:
    """Return the configured PMS adapter.

    Weekend mode uses the mock. ``open_dental_real`` is a stub for Iter 2.
    """
    adapter = get_settings().pms_adapter
    if adapter == "mock":
        return MockOpenDentalAdapter()
    if adapter == "open_dental_real":
        from app.adapters.open_dental.client import OpenDentalClient

        return OpenDentalClient()
    raise ValueError(f"Unknown PMS_ADAPTER: {adapter!r}")


__all__ = ["PMSAdapter", "MockOpenDentalAdapter", "get_pms_adapter"]
