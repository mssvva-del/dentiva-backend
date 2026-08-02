"""TCPA compliance guards for reactivation outreach (REACT-GUARD).

Legal basis (see dentiva-docs/_docs/LEGAL_COMPLIANCE_US.md §2): dental recall is a
"health care message" needing only prior express consent — but ONE promo word
("20% off whitening") reclassifies the whole message as marketing, which needs
prior express WRITTEN consent nobody has. Statutory damages are $500–$1,500 PER
TEXT, class-actionable. These guards make the safe path the only path:

  * content gate  — outbound reactivation copy must be treatment-only; anything
    promo-shaped is blocked BEFORE it reaches Twilio and logged loudly.
  * frequency cap — max 1 touch/day and 3 touches/week per PATIENT (across
    campaigns), per FCC 20-186 limits for exempted healthcare messages.

Quiet hours live in scheduling.py (9:00–20:00 patient-local — the conservative
window that also satisfies Florida FTSA's 8-to-8). Free-form opt-out parsing
lives in webhooks/twilio_sms.py.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reactivation import ReactivationTarget, ReactivationTouch

logger = logging.getLogger("dentiva.reactivation.compliance")

# ---------------------------------------------------------------------------
# Content gate — treatment-only copy.
# Patterns are deliberately broad (EN + ES): a false positive costs us one
# blocked message and a log line; a false negative costs $500–$1,500 per text.
# ---------------------------------------------------------------------------
_PROMO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(rx, re.IGNORECASE))
    for label, rx in [
        ("percent_off", r"\b\d{1,3}\s*%\s*(off|discount|de\s*descuento)"),
        ("discount", r"\bdiscount\w*\b|\bdescuento\w*\b|\brebaja\w*\b"),
        ("free_service", r"\bfree\s+(whitening|consult\w*|exam\w*|clean\w*|"
                         r"x-?rays?|gift)"),
        # Spanish word order puts the service first ("blanqueamiento gratis");
        # standalone "gratis" in dental outreach is promo essentially always.
        ("gratis", r"\bgratis\b"),
        ("special_offer", r"\bspecial\s+offer\b|\boferta\s+especial\b|\boferta\b"),
        ("promo", r"\bpromo\w*\b|\bpromoci[oó]n\w*\b"),
        ("deal_sale", r"\b(deal|sale)\b|\bventa\s+especial\b"),
        ("limited_time", r"\blimited\s+time\b|\btiempo\s+limitado\b"),
        ("coupon", r"\bcoupon\w*\b|\bcup[oó]n\w*\b"),
        ("save_money", r"\bsave\s+\$?\d+|\bahorr[ae]\w*\s+\$?\d+"),
        ("dollars_off", r"\$\s*\d+\s*(off|de\s*descuento)"),
    ]
)


def find_promo_violations(text: str) -> list[str]:
    """Labels of promo patterns found in ``text`` (empty = treatment-only, safe)."""
    return [label for label, rx in _PROMO_PATTERNS if rx.search(text or "")]


class PromoContentBlocked(Exception):
    """Raised when outbound reactivation copy fails the treatment-only gate."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__(f"promo content blocked: {', '.join(violations)}")
        self.violations = violations


def assert_treatment_only(text: str) -> None:
    """Belt-and-suspenders gate before ANY reactivation send. Built-in templates
    pass by construction (unit-tested); this protects future custom/clinic copy."""
    violations = find_promo_violations(text)
    if violations:
        # No PHI in the log — pattern labels only, never the message body.
        logger.error("reactivation content BLOCKED (TCPA promo gate): %s", violations)
        raise PromoContentBlocked(violations)


# ---------------------------------------------------------------------------
# Frequency caps — FCC 20-186: ≤1/day, ≤3/week per patient for exempted
# healthcare messages. Counted per PATIENT across campaigns (a patient enrolled
# twice must not receive double cadence).
# ---------------------------------------------------------------------------
MAX_TOUCHES_PER_DAY = 1
MAX_TOUCHES_PER_WEEK = 3

# A ceiling on what one practice can send in a day, counted in the DATABASE.
#
# The per-patient caps above stop us pestering one person; nothing stopped a
# runaway campaign or a bad patient import from dialling a whole list. The HTTP
# rate limiter can't help: it lives in process memory, so it multiplies by the
# number of instances and resets on every deploy — and the outbound worker
# doesn't go through HTTP at all. Every outbound call is real money and, in the
# US, a TCPA exposure per attempt.
#
# Counted from reactivation_touches, so the ceiling holds across instances,
# restarts and deploys. Deliberately generous: it is a blast radius, not a
# business rule. A practice legitimately working a recall list will not see it.
PRACTICE_MAX_TOUCHES_PER_DAY = 300

# Touches that count against the caps: anything that plausibly REACHED the
# patient. Failures/skips don't consume the allowance.
_COUNTED_STATUSES = ("sent", "queued")


async def frequency_block_reason(
    session: AsyncSession, patient_id: uuid.UUID, *, now: datetime | None = None
) -> str | None:
    """None = allowed; otherwise 'daily_limit' / 'weekly_limit'."""
    now = now or datetime.now(tz=UTC)

    async def _count(since: datetime) -> int:
        return (
            await session.execute(
                select(func.count())
                .select_from(ReactivationTouch)
                .join(ReactivationTarget,
                      ReactivationTouch.target_id == ReactivationTarget.id)
                .where(
                    ReactivationTarget.patient_id == patient_id,
                    ReactivationTouch.status.in_(_COUNTED_STATUSES),
                    ReactivationTouch.created_at >= since,
                )
            )
        ).scalar_one()

    if await _count(now - timedelta(days=1)) >= MAX_TOUCHES_PER_DAY:
        return "daily_limit"
    if await _count(now - timedelta(days=7)) >= MAX_TOUCHES_PER_WEEK:
        return "weekly_limit"
    return None


async def practice_daily_ceiling_reached(
    session: AsyncSession, practice_id: uuid.UUID, *, now: datetime | None = None
) -> bool:
    """Has this practice already sent its day's worth of outbound touches?

    Counted in the database rather than in memory: the limit has to survive a
    second instance, a restart and a deploy, because the thing it bounds is money
    leaving the account and calls landing on real phones.
    """
    now = now or datetime.now(tz=UTC)
    sent_today = (
        await session.execute(
            select(func.count())
            .select_from(ReactivationTouch)
            .where(
                ReactivationTouch.practice_id == practice_id,
                ReactivationTouch.status.in_(_COUNTED_STATUSES),
                ReactivationTouch.created_at >= now - timedelta(days=1),
            )
        )
    ).scalar_one()
    return sent_today >= PRACTICE_MAX_TOUCHES_PER_DAY
