"""PMS adapter package. Factory selects mock vs real based on settings.

Per-practice selection (Step 3): pass ``pms_system`` + ``customer_key`` from the
practice so one pilot clinic can run the real Open Dental adapter while everyone
else stays on the mock — keeping the global PMS_ADAPTER='mock' default safe.
"""

from app.adapters.open_dental.interface import PMSAdapter
from app.adapters.open_dental.mock import MockOpenDentalAdapter
from app.config import get_settings


def get_pms_adapter(
    *, pms_system: str | None = None, customer_key: str | None = None
) -> PMSAdapter:
    """Return a PMS adapter.

    Resolution order:
      1. Explicit ``pms_system`` (per-practice) — 'open_dental' → real client,
         else mock.
      2. Fallback to the global PMS_ADAPTER setting ('mock' | 'open_dental_real').

    ``customer_key`` (per-practice Open Dental CUSTOMER key) is passed to the real
    client when provided; otherwise the client falls back to the env default.
    """
    if pms_system == "open_dental":
        from app.adapters.open_dental.client import OpenDentalClient

        # Per-practice real OD REQUIRES an explicit customer key — never fall back
        # to the env default here, or one clinic could end up talking to another
        # clinic's Open Dental office. (The global open_dental_real path below may
        # use the env key, since that's the single-clinic/dev configuration.)
        if not customer_key:
            raise ValueError(
                "open_dental per-practice adapter requires an explicit customer_key"
            )
        return OpenDentalClient(customer_key=customer_key)
    if pms_system in (None, "mock", "none", "nexhealth"):
        # Global setting decides when no per-practice real OD is requested.
        adapter = get_settings().pms_adapter
        if adapter == "open_dental_real":
            from app.adapters.open_dental.client import OpenDentalClient

            return OpenDentalClient(customer_key=customer_key)
        return MockOpenDentalAdapter()
    # Unknown per-practice value → safe default (mock).
    return MockOpenDentalAdapter()


__all__ = ["PMSAdapter", "MockOpenDentalAdapter", "get_pms_adapter"]
