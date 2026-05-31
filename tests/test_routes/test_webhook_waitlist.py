"""Tests for the join_waitlist voice tool + cancellation backfill + waitlist API.

Exercises the Retell custom-tool payload shape ({call, name, args}) through
/webhooks/retell: a caller joins the waitlist, and a later cancellation marks
the oldest waiting entry as "notified" (a slot opened up). SMS is disabled in
tests, so send_* returns {"skipped": "sms_disabled"} (no network). The dashboard
endpoint is checked for tenant-scoped listing.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.waitlist_entry import WaitlistEntry
from tests.conftest import seed_practice

_FUTURE = "2099-12-15"
_PHONE_BOOK = "+15557770000"
_PHONE_WAIT = "+15558881111"


async def _book(client, *, phone=_PHONE_BOOK, date=_FUTURE):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "wl-call-1", "agent_id": "agent_wl"},
            "name": "book_appointment",
            "args": {
                "patient_first_name": "Maria",
                "patient_last_name": "Lopez",
                "patient_phone": phone,
                "procedure": "cleaning",
                "preferred_date": date,
                "preferred_time_window": "morning",
            },
        },
    )


async def _join_waitlist(client, *, phone=_PHONE_WAIT, first_name="Dana"):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "wl-call-1", "agent_id": "agent_wl"},
            "name": "join_waitlist",
            "args": {
                "patient_first_name": first_name,
                "patient_last_name": "Reed",
                "patient_phone": phone,
                "procedure": "cleaning",
                "preferred_date": "as soon as possible",
                "preferred_time_window": "morning",
                "notes": "flexible any morning",
            },
        },
    )


async def _cancel(client, *, phone=_PHONE_BOOK):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "wl-call-1", "agent_id": "agent_wl"},
            "name": "cancel_appointment",
            "args": {"patient_phone": phone, "reason": "out of town"},
        },
    )


async def test_join_waitlist_creates_entry(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Waitlist Dental", clerk_org_id="org_wl1", clerk_user_id="user_wl1"
    )
    resp = await _join_waitlist(client)
    assert resp.status_code == 200
    assert resp.json()["added"] is True

    await db_session.commit()
    entries = (
        await db_session.execute(
            select(WaitlistEntry).where(WaitlistEntry.practice_id == practice.id)
        )
    ).scalars().all()
    assert len(entries) == 1
    assert entries[0].status == "waiting"
    assert entries[0].procedure_type == "cleaning"

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "waitlist_joined")
        )
    ).scalars().all()
    assert len(audits) == 1


async def test_cancellation_backfills_oldest_waitlist_entry(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Backfill Dental", clerk_org_id="org_wl2", clerk_user_id="user_wl2"
    )
    # A booked patient + a different patient waiting for a sooner slot.
    assert (await _book(client)).json()["booked"] is True
    assert (await _join_waitlist(client)).json()["added"] is True

    # Cancelling the booking should notify the waitlisted patient.
    resp = await _cancel(client)
    assert resp.status_code == 200 and resp.json()["cancelled"] is True

    await db_session.commit()
    entry = (
        await db_session.execute(
            select(WaitlistEntry).where(WaitlistEntry.practice_id == practice.id)
        )
    ).scalar_one()
    assert entry.status == "notified"
    assert entry.notified_at is not None

    notified = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "waitlist_notified")
        )
    ).scalars().all()
    assert len(notified) == 1


async def test_cancel_with_empty_waitlist_is_graceful(client, db_session):
    await seed_practice(
        db_session, name="Empty WL Dental", clerk_org_id="org_wl3", clerk_user_id="user_wl3"
    )
    assert (await _book(client)).json()["booked"] is True

    resp = await _cancel(client)
    assert resp.status_code == 200 and resp.json()["cancelled"] is True

    await db_session.commit()
    notified = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "waitlist_notified")
        )
    ).scalars().all()
    assert notified == []


async def test_list_waitlist_endpoint(client, db_session):
    await seed_practice(
        db_session, name="WL API Dental", clerk_org_id="org_wl4", clerk_user_id="user_wl4"
    )
    assert (await _join_waitlist(client, first_name="Dana")).json()["added"] is True

    resp = await client.get(
        "/api/waitlist",
        headers={"X-Dev-Clerk-User-Id": "user_wl4", "X-Dev-Clerk-Org-Id": "org_wl4"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["waiting"] == 1
    entry = body["entries"][0]
    assert entry["status"] == "waiting"
    assert entry["patient_name_redacted"] == "Dana"
    assert entry["phone_last4"] == "1111"
    assert entry["procedure_type"] == "cleaning"


async def test_list_waitlist_requires_auth(client, db_session):
    await seed_practice(
        db_session, name="WL Auth Dental", clerk_org_id="org_wl5", clerk_user_id="user_wl5"
    )
    resp = await client.get("/api/waitlist")
    assert resp.status_code == 401
