"""Standing up two hundred clinics from a spreadsheet.

Every test here is a way a real import goes wrong. None of them are exotic: a
re-sent sheet, one bad row in the middle, Clerk giving up halfway, a phone
number that already belongs to somebody.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.practice import Practice
from app.services.bulk_onboarding import import_practices
from tests.conftest import seed_practice
from tests.test_routes.test_admin import _h, _internal

pytestmark = pytest.mark.asyncio


def _rows(n: int, *, prefix: str = "grp") -> list[dict]:
    return [
        {
            "external_ref": f"{prefix}-{i:03d}",
            "name": f"Group Dental {i:03d}",
            "timezone": "America/New_York",
            "address": f"{i} Main St",
            "pms_system": "open_dental",
            "customer_key": f"ck-{i:03d}",
        }
        for i in range(n)
    ]


def _clerk_ok():
    """A Clerk that always creates, with a distinct id per call."""
    seen: list[str] = []

    async def create_organization(*, name: str) -> str:
        seen.append(name)
        return f"org_bulk_{len(seen):04d}"

    return create_organization, seen


# ── The happy path, at size ────────────────────────────────────────────────


async def test_a_whole_estate_lands_in_one_call(db_session):
    create_org, _ = _clerk_ok()
    report = await import_practices(db_session, _rows(200), create_organization=create_org)

    assert (report.created, report.updated, report.failed) == (200, 0, 0)
    count = (await db_session.execute(
        select(func.count()).select_from(Practice)
        .where(Practice.external_ref.like("grp-%"))
    )).scalar_one()
    assert count == 200


async def test_each_clinic_keeps_its_own_credentials(db_session):
    """A group on Open Dental has one customer key PER OFFICE. Mixing them is
    how one location's calls end up reading another's calendar."""
    create_org, _ = _clerk_ok()
    await import_practices(db_session, _rows(25, prefix="cred"),
                           create_organization=create_org)

    practices = (await db_session.execute(
        select(Practice).where(Practice.external_ref.like("cred-%"))
    )).scalars().all()
    keys = {p.external_ref: p.pms_credentials["customer_key"] for p in practices}
    assert len(set(keys.values())) == 25
    for ref, key in keys.items():
        assert key == f"ck-{ref.split('-')[1]}"


# ── The sheet gets re-sent ─────────────────────────────────────────────────


async def test_re_importing_updates_instead_of_duplicating(db_session):
    """The whole reason external_ref exists. Without it the second run makes a
    second copy of every clinic, each with its own Clerk organisation, and the
    phone number points at whichever copy was created first."""
    create_org, calls = _clerk_ok()
    rows = _rows(30, prefix="again")
    await import_practices(db_session, rows, create_organization=create_org)

    for row in rows:
        row["name"] = row["name"] + " (renamed)"
    second = await import_practices(db_session, rows, create_organization=create_org)

    assert (second.created, second.updated, second.failed) == (0, 30, 0)
    assert len(calls) == 30, "a re-import must not create more Clerk organizations"
    total = (await db_session.execute(
        select(func.count()).select_from(Practice)
        .where(Practice.external_ref.like("again-%"))
    )).scalar_one()
    assert total == 30
    renamed = (await db_session.execute(
        select(Practice).where(Practice.external_ref == "again-000")
    )).scalar_one()
    assert renamed.name.endswith("(renamed)")


async def test_a_partial_sheet_does_not_wipe_what_it_omits(db_session):
    """A group corrects one column and re-sends. Absent must mean unchanged —
    otherwise the correction quietly blanks every clinic's address and phone."""
    create_org, _ = _clerk_ok()
    await import_practices(
        db_session,
        [{"external_ref": "keep-1", "name": "Keep Dental",
          "address": "1 Harbor Rd", "phone_number": "+19785551000",
          "pms_system": "eaglesoft", "location_id": "555"}],
        create_organization=create_org,
    )

    await import_practices(
        db_session,
        [{"external_ref": "keep-1", "name": "Keep Dental", "timezone": "America/Chicago"}],
        create_organization=create_org,
    )

    kept = (await db_session.execute(
        select(Practice).where(Practice.external_ref == "keep-1")
    )).scalar_one()
    await db_session.refresh(kept)
    assert kept.timezone == "America/Chicago"     # the correction applied
    assert kept.address == "1 Harbor Rd"          # and nothing else was lost
    assert kept.phone_number == "+19785551000"
    assert kept.pms_credentials["location_id"] == "555"


# ── One bad row ────────────────────────────────────────────────────────────


async def test_one_bad_row_does_not_discard_the_good_ones(db_session):
    """At 200 rows a partial failure is the normal outcome. Rolling the batch
    back over row 3 throws away every working clinic and leaves the operator
    with nothing to act on."""
    create_org, _ = _clerk_ok()
    rows = _rows(5, prefix="mixed")
    rows[2]["pms_system"] = "not-a-real-system"
    rows[3]["name"] = "   "

    report = await import_practices(db_session, rows, create_organization=create_org)

    assert (report.created, report.failed) == (3, 2)
    failed = {r.index: r.reason for r in report.rows if r.outcome == "failed"}
    assert "not-a-real-system" in failed[2]
    assert "name is required" in failed[3]


async def test_an_unknown_pms_is_refused_not_silently_blanked(db_session):
    """Coercing it to "none" would mark a clinic as having no practice software —
    indistinguishable from one that chose to skip, and nobody goes back to
    check."""
    create_org, _ = _clerk_ok()
    report = await import_practices(
        db_session,
        [{"external_ref": "bad-pms", "name": "Typo Dental", "pms_system": "eaglesof"}],
        create_organization=create_org,
    )
    assert report.failed == 1
    assert not (await db_session.execute(
        select(Practice).where(Practice.external_ref == "bad-pms")
    )).scalar_one_or_none()


async def test_a_row_without_an_external_ref_is_refused(db_session):
    """Letting it through would create a clinic that the next import cannot
    find, so the next run makes another one."""
    create_org, _ = _clerk_ok()
    report = await import_practices(
        db_session, [{"name": "Anonymous Dental"}], create_organization=create_org
    )
    assert report.failed == 1
    assert "external_ref is required" in report.rows[0].reason


# ── Clerk gives up halfway ─────────────────────────────────────────────────


async def test_clerk_failing_midway_leaves_no_orphans(db_session):
    """Clerk rate-limits large batches. A practice row without an organisation
    is an orphan: it looks real in every list, nobody can sign in to it, and
    when the owner later creates their own organisation a second practice
    appears beside it holding the calls.

    The rows that failed must be re-importable — which they are, precisely
    because nothing was written for them."""
    made = 0

    async def flaky_organization(*, name: str) -> str | None:
        nonlocal made
        made += 1
        return f"org_flaky_{made:04d}" if made <= 6 else None

    report = await import_practices(
        db_session, _rows(10, prefix="flaky"), create_organization=flaky_organization
    )

    assert (report.created, report.failed) == (6, 4)
    rows = (await db_session.execute(
        select(Practice).where(Practice.external_ref.like("flaky-%"))
    )).scalars().all()
    assert len(rows) == 6
    assert all(p.clerk_org_id for p in rows), "a practice without an org is an orphan"

    # And the failed tail imports cleanly on a retry, with no duplicates.
    create_org, _ = _clerk_ok()
    retry = await import_practices(
        db_session, _rows(10, prefix="flaky"), create_organization=create_org
    )
    assert (retry.created, retry.updated, retry.failed) == (4, 6, 0)


# ── Through the endpoint ───────────────────────────────────────────────────


async def test_the_endpoint_reports_per_row_and_still_returns_200(
    client, db_session, monkeypatch
):
    """A 4xx would tell the operator "the import failed" about a run that
    created most of the estate. The per-row outcomes are the answer."""
    await _internal(db_session, clerk_id="sa_bulk", role="super_admin")

    made = 0

    async def create_organization(*, name: str) -> str:
        nonlocal made
        made += 1
        return f"org_ep_{made:04d}"

    monkeypatch.setattr(
        "app.services.clerk_api.create_organization", create_organization
    )

    r = await client.post(
        "/api/admin/clinics/bulk",
        headers=_h("sa_bulk"),
        json={"clinics": [
            {"external_ref": "ep-1", "name": "Endpoint Dental One"},
            {"external_ref": "ep-2", "name": "Endpoint Dental Two",
             "pms_system": "nonsense"},
        ]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert body["failed"] == 1
    assert body["rows"][1]["reason"]


async def test_the_endpoint_is_admin_only(client, db_session):
    """A clinic user standing up two hundred practices is not a scenario."""
    await seed_practice(
        db_session, name="Not Staff", clerk_org_id="org_ns", clerk_user_id="u_ns"
    )
    r = await client.post(
        "/api/admin/clinics/bulk",
        headers={"X-Dev-Clerk-User-Id": "u_ns", "X-Dev-Clerk-Org-Id": "org_ns"},
        json={"clinics": [{"external_ref": "x", "name": "X"}]},
    )
    assert r.status_code in (401, 403)
