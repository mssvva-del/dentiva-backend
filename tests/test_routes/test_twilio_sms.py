"""Tests for the inbound Twilio SMS webhook (two-way texting) + opt-out.

Twilio POSTs application/x-www-form-urlencoded with From/To/Body. We classify
the reply and act: CONFIRM (audit), CANCEL (cancel soonest booking + waitlist
backfill), STOP/START (opt-out toggle). Outbound senders are stubbed so the
tests are deterministic regardless of the ambient Twilio config.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.webhooks.twilio_sms as twilio_sms
from app.db import set_tenant
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.waitlist_entry import WaitlistEntry
from tests.conftest import seed_practice

_PHONE = "+15557770000"
_WAIT_PHONE = "+15558881111"


@pytest.fixture(autouse=True)
def _stub_senders(monkeypatch):
    """No network: capture outbound sends instead of hitting Twilio."""
    sent: list[dict] = []

    async def _fake(**kwargs):
        sent.append(kwargs)
        return {"sent": True, "sid": "SMtest"}

    monkeypatch.setattr(twilio_sms, "send_cancellation_notice", _fake)
    monkeypatch.setattr(twilio_sms, "send_waitlist_opening", _fake)
    return sent


async def _seed_patient_booking(
    db_session, practice_id, *, phone=_PHONE, ext="sms-1", booking=True
):
    await set_tenant(db_session, practice_id)
    patient = Patient(
        id=uuid.uuid4(),
        practice_id=practice_id,
        pms_external_id=ext,
        first_name="Maria",
        last_name="Lopez",
        phone=phone,
    )
    db_session.add(patient)
    await db_session.flush()
    bk = None
    if booking:
        bk = Booking(
            id=uuid.uuid4(),
            practice_id=practice_id,
            patient_id=patient.id,
            appointment_at=datetime.now(tz=UTC) + timedelta(days=1),
            procedure_type="cleaning",
            provider_name="Dr. Smith",
            status="confirmed",
        )
        db_session.add(bk)
    await db_session.commit()
    return patient, bk


def _post(client, body, *, frm=_PHONE):
    return client.post(
        "/webhooks/twilio/sms",
        content=f"From={frm.replace('+', '%2B')}&To=%2B15550000000&Body={body}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


async def test_confirm_records_audit(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="SMS Dental", clerk_org_id="org_s1", clerk_user_id="user_s1"
    )
    await _seed_patient_booking(db_session, practice.id)

    resp = await _post(client, "CONFIRM")
    assert resp.status_code == 200
    assert "confirmed" in resp.text.lower()

    await db_session.commit()
    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "booking_confirmed_sms")
        )
    ).scalars().all()
    assert len(audits) == 1


async def test_cancel_cancels_booking_and_backfills(client, db_session, _stub_senders):
    practice, _ = await seed_practice(
        db_session, name="SMS Dental 2", clerk_org_id="org_s2", clerk_user_id="user_s2"
    )
    _, booking = await _seed_patient_booking(db_session, practice.id)
    # A waiting patient who should be notified when the slot frees up.
    await set_tenant(db_session, practice.id)
    waiter = Patient(
        id=uuid.uuid4(), practice_id=practice.id, pms_external_id="sms-wait",
        first_name="Dana", phone=_WAIT_PHONE,
    )
    db_session.add(waiter)
    await db_session.flush()
    db_session.add(
        WaitlistEntry(
            id=uuid.uuid4(), practice_id=practice.id, patient_id=waiter.id,
            procedure_type="cleaning", status="waiting",
        )
    )
    await db_session.commit()

    resp = await _post(client, "CANCEL")
    assert resp.status_code == 200
    assert "cancelled" in resp.text.lower()

    await db_session.commit()
    refreshed = (
        await db_session.execute(
            select(Booking).where(Booking.id == booking.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "cancelled"

    wl = (
        await db_session.execute(
            select(WaitlistEntry).where(WaitlistEntry.practice_id == practice.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert wl.status == "notified"

    # Both the cancellation confirmation and the waitlist opening were sent.
    assert len(_stub_senders) == 2


async def test_stop_opts_out_then_start_opts_in(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="SMS Dental 3", clerk_org_id="org_s3", clerk_user_id="user_s3"
    )
    patient, _ = await _seed_patient_booking(db_session, practice.id, booking=False)

    resp = await _post(client, "STOP")
    assert resp.status_code == 200
    await db_session.commit()
    await set_tenant(db_session, practice.id)
    p = (
        await db_session.execute(
            select(Patient).where(Patient.id == patient.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert p.sms_opt_out is True

    resp = await _post(client, "START")
    assert resp.status_code == 200
    await db_session.commit()
    await set_tenant(db_session, practice.id)
    p = (
        await db_session.execute(
            select(Patient).where(Patient.id == patient.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert p.sms_opt_out is False


async def test_unknown_number_is_polite(client, db_session):
    await seed_practice(
        db_session, name="SMS Dental 4", clerk_org_id="org_s4", clerk_user_id="user_s4"
    )
    resp = await _post(client, "CONFIRM", frm="+15550009999")
    assert resp.status_code == 200
    assert "couldn't match" in resp.text.lower()


async def test_unknown_keyword_gets_help(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="SMS Dental 5", clerk_org_id="org_s5", clerk_user_id="user_s5"
    )
    await _seed_patient_booking(db_session, practice.id, ext="sms-5", booking=False)
    resp = await _post(client, "hello%20there")
    assert resp.status_code == 200
    assert "reply confirm" in resp.text.lower()
