"""One price grid, and a lead that knows where it came from.

Three sources named three different grids: the site, the marketing table it
was meant to read, and the billing catalog that Stripe actually charges. A
clinic read "Core, $399" on the site and was offered "Front Desk, $499, 650
minutes" at checkout. The marketing table now mirrors the catalog, and the
first test here is what stops the next divergence from reaching a sale.

The second half: the site's demo form sends fields under its own names and
attaches first-touch attribution. Accepting them as they are means the site
changes one URL, and the inbox can say which channel earned each request.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from app.billing.plans import PLANS
from app.models.dentiva_staff import DentivaStaff
from app.models.lead import Lead
from app.models.user import User


def _grid_migration():
    """The migration that rewrites the marketing rows, loaded by path: tests build
    the schema with create_all, so its rows never exist here — but its numbers do."""
    import importlib.util
    import pathlib

    path = next(pathlib.Path("migrations/versions").glob("*_one_price_grid.py"))
    spec = importlib.util.spec_from_file_location("one_price_grid", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_marketing_grid_is_the_billing_catalog():
    """What the site advertises is what Stripe charges: same keys, prices,
    allowances and overage. The marketing rows come from a migration; this
    holds that migration to the catalog so the next divergence fails a build
    instead of a sale."""
    grid = {row[0]: row for row in _grid_migration()._GRID}
    assert set(grid) == set(PLANS), "the site and the bill name different plans"
    for key, plan in PLANS.items():
        _, name, monthly, annual, minutes, overage, *_ = grid[key]
        assert name == plan.name
        assert monthly == plan.monthly_price_cents, key
        assert minutes == plan.included_minutes, key
        assert overage == plan.overage_cents_per_min, key
        assert annual == plan.annual_monthly_equivalent_cents, f"{key}: annual price drifted"


async def test_the_public_endpoint_reads_that_grid(client, db_session):
    """The site is meant to read /api/pricing rather than hardcode numbers.
    Seed the rows the migration would write and read them back the way the
    site will."""
    import uuid as _uuid

    grid = _grid_migration()._GRID
    for key, name, monthly, annual, minutes, overage, react, per_loc, hl, sort in grid:
        await db_session.execute(text(
            "insert into pricing_plans (id, plan_key, name, monthly_cents, "
            "annual_monthly_cents, soft_cap_minutes, overage_cents_per_min, "
            "reactivation_level, per_location, highlight, sort_order, is_active, "
            "created_at, updated_at) values (:id, :key, :name, :m, :a, :min, "
            ":o, :r, :p, :h, :s, true, now(), now())"
        ), {"id": str(_uuid.uuid4()), "key": key, "name": name, "m": monthly, "a": annual,
            "min": minutes, "o": overage, "r": react, "p": per_loc, "h": hl, "s": sort})
    await db_session.commit()

    body = (await client.get("/api/pricing")).json()
    assert [p["key"] for p in body["plans"]] == ["overflow", "front_desk", "revenue", "multi"]
    front = next(p for p in body["plans"] if p["key"] == "front_desk")
    assert (front["monthly_cents"], front["soft_cap_minutes"]) == (49900, 650)
    assert front["highlight"] is True


async def test_the_demo_form_is_accepted_under_its_own_names(client, db_session):
    """practice, locations, referral, description, source_* — the names the
    site has used for months. The site changes one URL, not its form."""
    r = await client.post("/api/leads", json={
        "name": "Dr. Levin", "email": "levin@example.com", "phone": "+16175550100",
        "practice": "Harborside Dental", "locations": "3",
        "referral": "Dr. Patel", "description": "We miss calls after 5pm.",
        "source_landing": "/pricing/", "source_referrer": "google.com",
        "source_utm": "cpc:dental-answering", "submitted_from": "/demo/",
    })
    assert r.status_code == 200 and r.json() == {"ok": True}

    lead = (await db_session.execute(
        select(Lead).where(Lead.email == "levin@example.com")
    )).scalar_one()
    assert lead.clinic_name == "Harborside Dental"
    # The things the table has no column for lead the message — they are the
    # first thing sales wants to know.
    assert lead.message == "Locations: 3\nReferral: Dr. Patel\nWe miss calls after 5pm."
    assert (lead.landing_page, lead.referrer, lead.utm, lead.submitted_from) == (
        "/pricing/", "google.com", "cpc:dental-answering", "/demo/"
    )


async def test_the_inbox_shows_where_a_lead_came_from(client, db_session):
    u = User(id=uuid.uuid4(), clerk_user_id="user_sales_attr", practice_id=None,
             email="sales@dentovox.com", role="staff", is_internal=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(DentivaStaff(id=uuid.uuid4(), user_id=u.id, role="sales"))
    await db_session.commit()

    await client.post("/api/leads", json={
        "email": "attr@example.com", "source_utm": "linkedin:sept", "submitted_from": "/",
    })
    r = await client.get("/api/admin/leads", headers={
        "X-Dev-Clerk-User-Id": "user_sales_attr", "X-Dev-Clerk-Org-Id": "org_internal",
    })
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["email"] == "attr@example.com")
    assert row["utm"] == "linkedin:sept"
    assert row["submitted_from"] == "/"
