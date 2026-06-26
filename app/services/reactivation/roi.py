"""Reactivation ROI tracking (Phase 1, block 9).

The number that sells the product (spec 1.8): how much revenue the engine
recovered. Two pieces:

  * ``attribute_booking`` — when a dormant patient books (via the outbound call's
    write-back OR by replying to a reactivation text), tie that booking to their
    reactivation target so it counts as recovered. (The voice path sets this in
    block 7; this covers the SMS/inbound path.)
  * ``campaign_roi`` — the funnel + recovered revenue, for the dashboard:
    enrolled → contacted → booked, and $ recovered.

Recovered revenue uses each booked target's ``value_score`` (treatment value +
hygiene LTV, in cents) — the value we estimated when we prioritized them. Pure
reads over the reactivation tables; tenant-scoped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_tenant
from app.models.reactivation import ReactivationTarget

# Active states a booking can be attributed to (i.e. not already terminal).
_ATTRIBUTABLE = ("pending", "in_progress", "no_answer")


async def attribute_booking(
    session: AsyncSession,
    practice_id: uuid.UUID,
    patient_id: uuid.UUID,
    booking_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Tie a new booking to the patient's most recent active reactivation target,
    marking it 'booked'. Returns False if the patient has no active target (i.e.
    this booking wasn't driven by reactivation). Idempotent-ish: an already-booked
    target is left as-is (we don't double-attribute)."""
    now = now or datetime.now(tz=UTC)
    await set_tenant(session, practice_id)
    target = (
        await session.execute(
            select(ReactivationTarget)
            .where(
                ReactivationTarget.practice_id == practice_id,
                ReactivationTarget.patient_id == patient_id,
                ReactivationTarget.status.in_(_ATTRIBUTABLE),
            )
            .order_by(ReactivationTarget.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if target is None:
        return False
    target.status = "booked"
    target.booking_id = booking_id
    target.next_touch_at = None  # success — stop contacting
    await session.commit()
    return True


async def campaign_roi(
    session: AsyncSession,
    practice_id: uuid.UUID,
    *,
    campaign_id: uuid.UUID | None = None,
) -> dict:
    """Funnel + recovered revenue for a practice (or one campaign).

    revenue_recovered_cents = Σ value_score over booked targets. conversion_rate =
    booked / contacted (0 when nobody contacted yet)."""
    await set_tenant(session, practice_id)
    q = select(ReactivationTarget).where(ReactivationTarget.practice_id == practice_id)
    if campaign_id is not None:
        q = q.where(ReactivationTarget.campaign_id == campaign_id)
    targets = (await session.execute(q)).scalars().all()

    enrolled = len(targets)
    contacted = sum(1 for t in targets if t.touches_count > 0)
    booked = [t for t in targets if t.status == "booked"]
    revenue_cents = sum(int(t.value_score) for t in booked)
    return {
        "enrolled": enrolled,
        "contacted": contacted,
        "booked": len(booked),
        "no_answer": sum(1 for t in targets if t.status == "no_answer"),
        "opted_out": sum(1 for t in targets if t.status == "opted_out"),
        "revenue_recovered_cents": revenue_cents,
        "revenue_recovered_dollars": round(revenue_cents / 100, 2),
        # booked per contacted — the headline conversion of the program.
        "conversion_rate": round(len(booked) / contacted, 4) if contacted else 0.0,
    }
