"""Provision a Dentovox phone number for one clinic (NUM-1).

Retell buys and manages the number itself (`create-phone-number`), so we don't
run a Twilio SIP trunk to hand it over — one API call gets a number that is
already bound to our inbound agent and points at our inbound webhook.

Why per-clinic: inbound routing identifies the practice by the number that was
dialled. With one shared number two clinics are indistinguishable, so the router
refuses to guess (correctly — guessing would mean one clinic's call landing in
another's records). A number per clinic makes routing deterministic.

The number is bought in the CLINIC'S OWN area code where we can tell what that
is: a local number is what patients expect to see, and forwarding to a local
number avoids long-distance surprises on older plans.
"""

from __future__ import annotations

import logging
import re

from app.config import get_settings
from app.models.practice import Practice
from app.services.retell_admin import RetellError, RetellNotConfigured, _request

logger = logging.getLogger("dentiva.telephony.provision")

# A number costs money every month, for as long as it exists. Most self-serve
# signups never become customers, and an unused number doesn't go away by itself —
# a year of tyre-kickers is a permanent monthly bill. So a number follows a
# COMMERCIAL COMMITMENT, not a signup: the practice must have been moved past raw
# onboarding (trial/pilot by us today, payment-backed once Stripe is live).
#
# Nothing is lost for the prospect: the browser demo already lets them talk to
# their own AI receptionist, using their own clinic's data, before any number
# exists. The number is what turns that into a live phone line.
PROVISIONABLE_STATUSES = frozenset({"trial", "pilot", "active"})


class NotEntitledToNumber(Exception):
    """Practice hasn't reached a paid/approved state — no number for it yet."""


def area_code_of(*numbers: str | None) -> int | None:
    """US area code from the first usable number given, else None.

    Accepts any format ('+1 (718) 786-4175', '7187864175'). A 10-digit number is
    NANP without the country code; 11 digits starting with 1 includes it.
    """
    for raw in numbers:
        digits = re.sub(r"\D", "", raw or "")
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            code = int(digits[:3])
            # NANP area codes never start with 0 or 1.
            if 200 <= code <= 999:
                return code
    return None


async def provision_number_for_practice(
    practice: Practice, *, transport=None
) -> str:
    """Buy a Dentovox number for this practice and return it in E.164.

    Bound to our inbound agent and inbound webhook at creation time, so the
    number answers correctly from the first call. Raises RetellNotConfigured /
    RetellError — the caller maps those to a clean HTTP status.
    """
    if practice.status not in PROVISIONABLE_STATUSES:
        raise NotEntitledToNumber(
            f"practice status '{practice.status}' is not entitled to a number"
        )
    settings = get_settings()
    if not settings.retell_agent_id:
        raise RetellNotConfigured("RETELL_AGENT_ID not set — nothing to bind the number to")

    payload: dict = {
        # Bind on creation: a number that rings with no agent is worse than none.
        #
        # `weight` is REQUIRED by Retell — they added it, and without it every
        # purchase returns 400 "must have required property 'weight'". Nothing on
        # our side changed; buying a number simply stopped working one day, and
        # the clinic saw "Retell refused to sell a number just now". It matters
        # only when several agents share one number, splitting traffic between
        # them; with a single agent any positive value routes everything to it.
        "inbound_agents": [{"agent_id": settings.retell_agent_id, "weight": 1}],
        "nickname": f"Dentovox · {practice.name}"[:60],
        # Retell calls this BEFORE answering; we return this clinic's dynamic
        # variables so the agent greets with the right practice name and KB.
        "inbound_webhook_url": f"{settings.public_base_url}/webhooks/retell/inbound",
    }
    # Prefer the clinic's own area code; fall back to Retell's choice if we can't
    # tell (better a working number than a failed setup).
    area = area_code_of(practice.phone_number, practice.transfer_phone_number)
    if area is not None:
        payload["area_code"] = area

    try:
        data = await _request("POST", "/create-phone-number", payload, transport=transport)
    except RetellError as exc:
        # A specific area code can simply be sold out — retry once, any area code.
        if area is not None and exc.status_code and 400 <= exc.status_code < 500:
            logger.warning(
                "provision: area code %s unavailable for practice %s, retrying anywhere",
                area, practice.id,
            )
            payload.pop("area_code", None)
            data = await _request("POST", "/create-phone-number", payload,
                                  transport=transport)
        else:
            raise

    number = (data or {}).get("phone_number")
    if not number:
        raise RetellError("Retell returned no phone_number", status_code=502)
    logger.info("provision: practice=%s got number (area=%s)", practice.id, area)
    return str(number)
