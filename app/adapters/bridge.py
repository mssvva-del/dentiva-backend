"""Which bridge answers for this practice — and whether one can.

A dental practice runs Eaglesoft, Dentrix, Open Dental or Curve, and almost none
of them will talk to us directly: Patterson wants $3–5K to join their programme
for Eaglesoft, Dentrix wants $5,000. So we reach them through an aggregator, and
there are two — Kolla at a listed $19 per location, NexHealth at $75.

The brand of PMS and the bridge that reaches it are separate facts. A practice
says "we run Eaglesoft"; both bridges can reach Eaglesoft, and which one we use
is our commercial decision, not theirs. So selection is by configured
credentials, preferring the cheaper bridge, rather than by the name of the PMS.

That holds while every practice goes through the same bridge, which is true today
and stops being true the moment one clinic is on Kolla and another on NexHealth.
At that point this needs a column on practices, not more cleverness here — and
the shape below is deliberately the one that becomes a per-practice lookup with a
single line changed.
"""

from __future__ import annotations

from app.config import get_settings
from app.models.practice import Practice

# A practice that has not picked a real system, or picked our mock. Onboarding
# lets a clinic proceed before the PMS question is settled, so this is a normal
# state and not an error.
_NO_PMS = {"", "none", "mock", "other"}


def _kolla_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.kolla_api_key
        and (settings.kolla_consumer_id or settings.kolla_connector_id)
    )


def _nexhealth_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.nexhealth_api_key
        and settings.nexhealth_subdomain
        and settings.nexhealth_location_id
    )


def bridge_name(practice: Practice) -> str | None:
    """"kolla", "nexhealth", or None when nothing can answer for this practice."""
    if (practice.pms_system or "").strip().lower() in _NO_PMS:
        return None
    if _kolla_configured():
        return "kolla"
    if _nexhealth_configured():
        return "nexhealth"
    return None


def pms_client_for(practice: Practice):
    """A client for this practice's PMS, or None.

    None is a normal answer and every caller already handles it by falling back
    to our own book. Returning a client that cannot authenticate would instead
    fail in the middle of a call, which is the same outcome told to the patient
    much later and much worse.
    """
    name = bridge_name(practice)
    if name == "kolla":
        from app.adapters.kolla.client import KollaClient

        return KollaClient()
    if name == "nexhealth":
        from app.adapters.nexhealth.client import NexHealthClient

        return NexHealthClient()
    return None
