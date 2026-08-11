"""The monitoring clinic makes itself, and must be safe to make twice.

There is no screen for creating a clinic — practices arrive through onboarding,
which is right, and which a monitoring tenant should not go through. The
alternatives were somebody running SQL against production by hand or an engineer
holding its credentials to do it for them. One environment variable beats both.

Which puts the burden here: this runs on every boot, in production, against the
real database. It has to be idempotent, it has to be barren, and it must never
stop the application starting.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.practice import Practice
from app.services.canary import CANARY_NAME, ensure_canary_practice


async def test_it_creates_the_clinic_once_and_only_once(db_session):
    """Railway redeploys on every merge, so this runs several times a day. A
    second canary would break the routing fallback it was carefully kept out of
    — two canaries and one real clinic still counts as three practices."""
    first = await ensure_canary_practice(db_session)
    second = await ensure_canary_practice(db_session)
    assert first == second

    count = (await db_session.execute(
        select(func.count()).select_from(Practice).where(Practice.is_canary.is_(True))
    )).scalar_one()
    assert count == 1


async def test_the_canary_has_no_pms(db_session):
    """The load-bearing property. Everything the monitor books must stop in our
    database — a canary wired to a real bridge writes test appointments into
    somebody's calendar, which is the thing it exists to avoid."""
    canary_id = await ensure_canary_practice(db_session)
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()
    assert practice.pms_system == "none"
    assert practice.is_canary is True


async def test_it_does_not_text_anyone(db_session):
    """It would otherwise page a phone number nobody owns every ten minutes, for
    as long as the monitor runs."""
    canary_id = await ensure_canary_practice(db_session)
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()
    assert practice.booking_alerts_enabled is False


async def test_it_is_open_during_the_week(db_session):
    """A clinic that is never open offers no slots, and the monitor would be
    testing an empty branch while reporting success."""
    canary_id = await ensure_canary_practice(db_session)
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()
    assert {"mon", "tue", "wed", "thu", "fri"} <= set(practice.business_hours)
    assert practice.name == CANARY_NAME


async def test_a_renamed_canary_is_still_the_canary(db_session):
    """Idempotent by the flag, not the name. The name is the part a human might
    reasonably change; the flag is the part that means something."""
    canary_id = await ensure_canary_practice(db_session)
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()
    practice.name = "Renamed By Somebody"
    await db_session.commit()

    assert await ensure_canary_practice(db_session) == canary_id


async def test_the_canary_owns_the_number_the_monitor_dials(db_session):
    """Routing matches ai_phone_number exactly, so this is what makes a synthetic
    call land on the canary and nowhere else.

    Without it the call falls through to "the only practice in the database" —
    and that practice is a real clinic. A monitoring booking in a live calendar
    is the precise thing the canary exists to prevent, and arriving there by
    accident would be worse than not monitoring at all.
    """
    from app.services.canary import CANARY_NUMBER

    canary_id = await ensure_canary_practice(db_session)
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()
    assert practice.ai_phone_number == CANARY_NUMBER


async def test_a_canary_missing_its_number_is_repaired(db_session):
    """One already exists in production, created before this number did. Assuming
    otherwise would leave synthetic bookings pointed at a real clinic."""
    from app.services.canary import CANARY_NUMBER

    canary_id = await ensure_canary_practice(db_session)
    practice = (await db_session.execute(
        select(Practice).where(Practice.id == canary_id)
    )).scalar_one()
    practice.ai_phone_number = None
    await db_session.commit()

    assert await ensure_canary_practice(db_session) == canary_id
    await db_session.refresh(practice)
    assert practice.ai_phone_number == CANARY_NUMBER
