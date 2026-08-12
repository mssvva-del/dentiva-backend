"""Which bridge answers for this practice — and whether one can.

A dental practice runs Eaglesoft, Dentrix, Open Dental or Curve, and almost none
of them will talk to us directly: Patterson wants $3–5K to join their programme
for Eaglesoft, Dentrix wants $5,000. So we reach them through an aggregator, and
there are two — Kolla at a listed $19 per location, NexHealth at $75.

The brand of PMS and the bridge that reaches it are separate facts. A practice
says "we run Eaglesoft"; both bridges can reach Eaglesoft, and which one we use
is our commercial decision, not theirs. So selection is by configured
credentials, not by the name of the PMS.

Credentials come from one of two places, in this order:

  1. **The practice's own row.** ``practices.pms_credentials`` names its bridge
     and carries that bridge's keys. This is the only answer that stays correct
     with more than one clinic on a PMS, and the only one settable without a
     redeploy.
  2. **The environment**, when ``PMS_ENV_PRACTICE_ID`` says the environment
     belongs to this practice (or names nobody, i.e. a single-clinic
     deployment). NEXHEALTH_*/KOLLA_* describe ONE location; applying them to
     whoever asks is how two clinics quietly become one.

No credentials is a normal answer, not an error: the agent falls back to our own
book, which is honest, rather than to somebody else's calendar, which is not.
"""

from __future__ import annotations

from app.config import get_settings
from app.models.practice import Practice

# A practice that has not picked a real system, or picked our mock. Onboarding
# lets a clinic proceed before the PMS question is settled, so this is a normal
# state and not an error.
_NO_PMS = {"", "none", "mock", "other"}

# What each bridge cannot work without. A half-filled set is treated as no
# credentials at all: a client built without a location id does not fail at
# construction, it fails mid-call, which is the same bad news delivered to a
# patient instead of to us.
REQUIRED_FIELDS = {
    "nexhealth": ("api_key", "subdomain", "location_id"),
    "kolla": ("api_key",),  # plus one of consumer_id / connector_id, checked below
}


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


def practice_credentials(practice: Practice) -> dict | None:
    """This practice's own bridge credentials, if they are complete.

    Incomplete is deliberately the same as absent. The alternative — falling
    through to the environment because one field is missing — would reach for
    another clinic's location exactly when this clinic's configuration is half
    done, which is the moment somebody is most likely to be watching the wrong
    screen.
    """
    creds = getattr(practice, "pms_credentials", None)
    if not isinstance(creds, dict):
        return None
    bridge = str(creds.get("bridge") or "").strip().lower()
    if bridge not in REQUIRED_FIELDS:
        return None
    if not all(str(creds.get(field) or "").strip() for field in REQUIRED_FIELDS[bridge]):
        return None
    if bridge == "kolla" and not (creds.get("consumer_id") or creds.get("connector_id")):
        # Without one of these a Kolla request spans every practice on the
        # connector. That is never what a patient-facing call wants.
        return None
    return {**creds, "bridge": bridge}


def _env_credentials_are_for(practice: Practice) -> bool:
    """May this practice use the credentials in the environment?

    NEXHEALTH_* and KOLLA_* name ONE clinic's location or linked account. They
    used to apply to any practice that had picked a PMS, so the second clinic to
    finish onboarding would have had its agent read the FIRST clinic's openings
    aloud and, with writes enabled, book its patients into the first clinic's
    chairs. Nothing would have errored — the two clinics would simply have been
    one clinic.

    Binding them to a named practice makes that impossible to reach by accident.
    An unset id keeps a single-clinic deployment working, which is every
    deployment today, and stops being enough the moment a second practice picks
    a PMS — which is what the per-practice column above is for.
    """
    named = (get_settings().pms_env_practice_id or "").strip()
    return not named or named == str(practice.id)


def bridge_name(practice: Practice) -> str | None:
    """"kolla", "nexhealth", or None when nothing can answer for this practice."""
    if (practice.pms_system or "").strip().lower() in _NO_PMS:
        return None
    own = practice_credentials(practice)
    if own is not None:
        return own["bridge"]
    if not _env_credentials_are_for(practice):
        # Another clinic's credentials are the only ones here. No PMS is the
        # correct answer: the agent falls back to our own book, which is honest,
        # rather than reading somebody else's calendar, which is not.
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
    own = practice_credentials(practice)
    # Only pass through the practice's own values when they are the ones that
    # chose the bridge. Mixing this clinic's key with the environment's location
    # id would authenticate fine and read the wrong calendar.
    fields = own if own and own["bridge"] == name else {}
    if name == "kolla":
        from app.adapters.kolla.client import KollaClient

        return KollaClient(
            api_key=fields.get("api_key") or None,
            connector_id=fields.get("connector_id") or None,
            consumer_id=fields.get("consumer_id") or None,
        )
    if name == "nexhealth":
        from app.adapters.nexhealth.client import NexHealthClient

        return NexHealthClient(
            api_key=fields.get("api_key") or None,
            subdomain=fields.get("subdomain") or None,
            location_id=fields.get("location_id") or None,
        )
    return None
