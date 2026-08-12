"""The monitoring clinic, created by the application rather than by a person.

A canary practice is the only way to answer "does a call still end in a booking?"
without putting a patient who does not exist into a real clinic's calendar. But
it is a row in production, and there is no screen for making one — clinics arrive
through onboarding, which is correct and which a monitoring tenant should not go
through.

The remaining options were somebody running SQL against the production database
by hand, or an engineer holding its credentials to do it for them. Both are worse
than one environment variable, so the app does it itself: set
CANARY_PRACTICE_ENABLED and the row appears, idempotently, on the next boot.

It is created deliberately barren:

  * pms_system "none", so every booking the monitor makes stops in our database.
    That is the load-bearing property — a canary wired to a real bridge writes
    test appointments into somebody's calendar, which is the exact thing the
    canary exists to avoid.
  * ordinary business hours, because a clinic that is never open offers no slots
    and the monitor would be testing an empty branch.
  * booking_alerts_enabled off, so it does not text anyone every ten minutes.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.models.practice import Practice

logger = logging.getLogger("dentiva.canary")

CANARY_ORG_ID = "org_dentovox_canary"
CANARY_NAME = "Dentovox Monitoring"

# The number the monitor "dials". Routing matches practices.ai_phone_number
# exactly, so this is what makes a synthetic call land on the canary and nowhere
# else — and that has to be deterministic, because the fallback it would
# otherwise hit picks the only real clinic in the database. A monitoring booking
# in a live practice's calendar is precisely what the canary exists to prevent,
# and getting there by accident would be worse than not monitoring at all.
#
# Deliberately undialable: +1 000 is not an assignable area code, so no human
# reaches it and no carrier routes it. It is a routing key wearing the shape of
# a phone number.
CANARY_NUMBER = "+10000000000"

# The clinic's "own" line, and where an urgent callback would be paged. Both are
# in the same reserved block, and app/services/sms.py never dials it.
#
# They exist because their ABSENCE was the loud thing. A practice with no number
# to page raises urgent_callback_unpageable, and the monitor escalates a
# synthetic urgent callback every run — so production reported "degraded" around
# the clock over a page nobody was ever supposed to receive. An alert that is
# always on is an alert nobody reads.
CANARY_CLINIC_NUMBER = "+10000000002"

# Monday to Friday, a normal day. The monitor books against these, so they have
# to be hours a slot can actually fall inside.
_HOURS = {
    day: {"open": "09:00", "close": "17:00"}
    for day in ("mon", "tue", "wed", "thu", "fri")
}


async def ensure_canary_practice(session) -> uuid.UUID | None:
    """Create the monitoring clinic if it is missing. Returns its id.

    Idempotent by the flag rather than by the name, because the name is the part
    a human might reasonably change and the flag is the part that means
    something. Runs on the platform connection: practices is where tenancy is
    decided, so there is no tenant to bind yet.
    """
    existing = (await session.execute(
        select(Practice).where(Practice.is_canary.is_(True))
    )).scalars().first()
    if existing is not None:
        # Repair rather than assume: a canary created before it had a routing
        # number would send every synthetic call down the fallback path, into
        # whichever real clinic is the only one in the database.
        changed = False
        if existing.ai_phone_number != CANARY_NUMBER:
            existing.ai_phone_number = CANARY_NUMBER
            changed = True
        if not existing.phone_number:
            # A canary created before these existed pages nobody, and says so
            # once every ten minutes.
            existing.phone_number = CANARY_CLINIC_NUMBER
            existing.transfer_phone_number = CANARY_CLINIC_NUMBER
            changed = True
        if changed:
            await session.commit()
            logger.info("canary practice repaired")
        return existing.id

    practice = Practice(
        id=uuid.uuid4(),
        clerk_org_id=CANARY_ORG_ID,
        name=CANARY_NAME,
        timezone="America/New_York",
        # No PMS. This is the property everything else rests on.
        pms_system="none",
        ai_phone_number=CANARY_NUMBER,
        phone_number=CANARY_CLINIC_NUMBER,
        transfer_phone_number=CANARY_CLINIC_NUMBER,
        languages_enabled=["en"],
        business_hours=_HOURS,
        is_canary=True,
        status="active",
        # It would otherwise text a phone number nobody owns, every ten minutes,
        # for as long as the monitor runs.
        booking_alerts_enabled=False,
    )
    session.add(practice)
    await session.commit()
    logger.info("canary practice created: %s", practice.id)
    return practice.id
