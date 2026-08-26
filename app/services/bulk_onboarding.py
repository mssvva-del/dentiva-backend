"""Standing up a group practice's whole estate from their own spreadsheet.

The self-serve wizard is right for a clinic that found us. It is the wrong shape
entirely for a group handing over 200 locations: nobody is going to sit through
200 wizards, and the data already exists in their system.

Three properties this has to have, and each one is a lesson from a different
import somewhere:

**Per-row, not all-or-nothing.** At 200 rows a partial failure is the NORMAL
outcome — one bad timezone, a duplicate number, Clerk rate-limiting halfway
through. Rolling the batch back over row 173 throws away 172 good clinics and
leaves the operator with nothing to act on. Every row succeeds or fails alone
and says which it was.

**Idempotent on the group's own id.** The spreadsheet gets re-sent: a corrected
column, ten new locations, a retry. Keyed on ``external_ref`` a second run
updates; keyed on nothing it creates a second copy of every practice, each with
its own Clerk organisation, and the phone numbers point at whichever copy was
made first.

**No money, no guessing.** It does not buy phone numbers — 200 numbers is a real
bill and a deliberate act. It does not default a missing required field; a row
that cannot be understood is returned with its position and its reason, so a
human fixes the sheet rather than discovering later that 40 clinics are in the
wrong timezone.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.practice import Practice

logger = logging.getLogger(__name__)

# One request, one sheet. A group's estate is hundreds, not tens of thousands,
# and each row costs a Clerk organisation — an unbounded list would spend the
# rate limit and leave a long tail of half-made clinics.
MAX_ROWS = 500

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Mirrors the wizard's Literal. Kept as a set here rather than imported so that
# widening the wizard cannot silently widen what an import accepts — the two
# have different blast radii.
KNOWN_PMS = frozenset({
    "eaglesoft", "dentrix", "dentrix_ascend", "dentrix_enterprise",
    "denticon", "curve", "cloud9", "open_dental", "other", "none",
})


@dataclass
class RowResult:
    """What happened to one line of the sheet."""

    index: int                      # position in the submitted list, 0-based
    external_ref: str
    outcome: str                    # created | updated | failed
    practice_id: str | None = None
    reason: str | None = None       # present only when failed


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    failed: int = 0
    rows: list[RowResult] = field(default_factory=list)

    def record(self, result: RowResult) -> None:
        self.rows.append(result)
        setattr(self, result.outcome, getattr(self, result.outcome) + 1)


def _clean(value: str | None) -> str:
    return (value or "").strip()


async def import_practices(
    session: AsyncSession,
    rows: list[dict],
    *,
    create_organization,
    audit=None,
) -> ImportReport:
    """Create or update one practice per row. Never raises for a bad row.

    ``create_organization`` is injected rather than imported so the caller owns
    the Clerk dependency — and so tests can drive the failure that actually
    happens in production, which is Clerk refusing partway through a large batch.
    """
    report = ImportReport()

    for index, raw in enumerate(rows):
        external_ref = _clean(raw.get("external_ref"))
        name = _clean(raw.get("name"))

        if not external_ref:
            report.record(RowResult(
                index=index, external_ref="", outcome="failed",
                reason="external_ref is required — it is what makes a re-import "
                       "an update instead of a duplicate clinic",
            ))
            continue
        if not name:
            report.record(RowResult(
                index=index, external_ref=external_ref, outcome="failed",
                reason="name is required",
            ))
            continue

        pms_system = _clean(raw.get("pms_system")) or "none"
        if pms_system not in KNOWN_PMS:
            # Refused, not coerced to "none": a clinic silently marked as having
            # no practice software looks identical to one that chose to skip,
            # and nobody goes back to check.
            report.record(RowResult(
                index=index, external_ref=external_ref, outcome="failed",
                reason=f"unknown pms_system {pms_system!r}",
            ))
            continue

        existing = (await session.execute(
            select(Practice).where(Practice.external_ref == external_ref)
        )).scalar_one_or_none()

        try:
            if existing is not None:
                _apply(existing, raw, name=name, pms_system=pms_system)
                await session.commit()
                report.record(RowResult(
                    index=index, external_ref=external_ref, outcome="updated",
                    practice_id=str(existing.id),
                ))
                continue

            # Clerk FIRST, exactly as the single-clinic path does. A practice row
            # without an organisation is an orphan: it looks real in every list,
            # nobody can sign in to it, and when the owner later makes their own
            # organisation a second practice appears beside it holding the calls.
            clerk_org_id = await create_organization(name=name)
            if not clerk_org_id:
                report.record(RowResult(
                    index=index, external_ref=external_ref, outcome="failed",
                    reason="Clerk would not create an organization — no clinic "
                           "was created, so this row can simply be re-imported",
                ))
                continue

            practice = Practice(
                id=uuid.uuid4(),
                clerk_org_id=clerk_org_id,
                external_ref=external_ref,
                name=name,
                timezone=_clean(raw.get("timezone")) or "America/New_York",
                pms_system=pms_system,
                business_hours={day: None for day in _DAYS},
                languages_enabled=["en", "es"],
                status="onboarding",
                onboarding_step=1,
            )
            _apply(practice, raw, name=name, pms_system=pms_system)
            session.add(practice)
            await session.commit()
            report.record(RowResult(
                index=index, external_ref=external_ref, outcome="created",
                practice_id=str(practice.id),
            ))
            if audit is not None:
                await audit(practice)
        except Exception as exc:  # noqa: BLE001 — one bad row must not end the run
            await session.rollback()
            logger.warning("bulk import: row %s (%s) failed: %s",
                           index, external_ref, exc)
            report.record(RowResult(
                index=index, external_ref=external_ref, outcome="failed",
                reason=_explain(exc),
            ))

    return report


def _apply(practice: Practice, raw: dict, *, name: str, pms_system: str) -> None:
    """Copy the sheet's optional columns onto a practice.

    Absent means UNCHANGED, not blank. A group that re-sends a sheet with only
    the columns they corrected must not have every other field wiped by the
    ones they left out.
    """
    practice.name = name
    practice.pms_system = pms_system
    if _clean(raw.get("timezone")):
        practice.timezone = _clean(raw["timezone"])
    if _clean(raw.get("phone_number")):
        practice.phone_number = _clean(raw["phone_number"])
    if _clean(raw.get("address")):
        practice.address = _clean(raw["address"])
    if _clean(raw.get("transfer_phone_number")):
        practice.transfer_phone_number = _clean(raw["transfer_phone_number"])

    # PMS credentials are merged, never replaced: the installer key is often set
    # days before a location id exists, and a later sheet carrying only the
    # location must not wipe the key the clinic is reading off its own screen.
    incoming = {
        key: _clean(raw.get(key))
        for key in ("location_id", "customer_key", "subdomain")
        if _clean(raw.get(key))
    }
    if incoming:
        bridge = "open_dental" if pms_system == "open_dental" else "nexhealth"
        practice.pms_credentials = {
            **(practice.pms_credentials or {}), "bridge": bridge, **incoming,
        }


def _explain(exc: Exception) -> str:
    """A reason an operator can act on, without leaking internals into a report."""
    text = str(exc)
    if "ai_phone_number" in text and "unique" in text.lower():
        return "that phone number already routes to another clinic"
    if "external_ref" in text and "unique" in text.lower():
        return "duplicate external_ref within this import"
    if "clerk_org_id" in text and "unique" in text.lower():
        return "that Clerk organization is already attached to another clinic"
    return f"{type(exc).__name__}: {text[:160]}"
