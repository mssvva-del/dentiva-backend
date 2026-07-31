"""transfer_to_human escalates as an urgent callback — it never bridges a call.

Retell only connects the parties for its NATIVE transfer_call tool. Ours is
`type: custom`, so the old "transfer_initiated" + number answer made the agent
promise a connection that could not happen. These tests pin the honest contract.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db import set_tenant
from app.models.callback_request import CallbackRequest
from app.models.practice import Practice
from tests.conftest import seed_practice


async def _transfer(client):
    return await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-call-1", "agent_id": "agent_tr"},
            "name": "transfer_to_human",
            "args": {"reason": "caller asked for a person"},
        },
    )


async def test_escalation_queues_urgent_callback_not_a_bridge(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Transfer Dental", clerk_org_id="org_tr1", clerk_user_id="user_tr1"
    )
    practice.transfer_phone_number = "+15551239999"
    practice_id = practice.id  # grab before commit expires the instance
    await db_session.commit()

    resp = await _transfer(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "callback_logged"
    # A number in the answer is what made the agent say "connecting you".
    assert "transfer_number" not in body

    await db_session.commit()
    db_session.expire_all()
    await set_tenant(db_session, practice_id)
    cb = (await db_session.execute(select(CallbackRequest))).scalars().all()
    assert len(cb) == 1 and cb[0].urgent is True


async def test_practice_me_exposes_transfer_number(client, db_session):
    practice, _ = await seed_practice(
        db_session, name="Transfer Dental 3", clerk_org_id="org_tr3", clerk_user_id="user_tr3"
    )
    resp = await client.patch(
        "/api/practice/me",
        json={"transfer_phone_number": "+15550001234"},
        headers={"X-Dev-Clerk-User-Id": "user_tr3", "X-Dev-Clerk-Org-Id": "org_tr3"},
    )
    assert resp.status_code == 200
    assert resp.json()["transfer_phone_number"] == "+15550001234"

    db_practice = (
        await db_session.execute(select(Practice).where(Practice.id == practice.id))
    ).scalar_one()
    assert db_practice.transfer_phone_number == "+15550001234"


async def test_urgent_callback_pages_the_clinic(client, db_session, monkeypatch):
    """A row in the dashboard is not a page — the team must be told."""
    from app.webhooks import retell as retell_mod

    paged: list[dict] = []

    async def _fake_page(**kwargs):
        paged.append(kwargs)
        return {"sent": True}

    monkeypatch.setattr(retell_mod, "page_clinic_urgent_callback", _fake_page)

    practice, _ = await seed_practice(
        db_session, name="Paged Dental", clerk_org_id="org_pg1", clerk_user_id="user_pg1"
    )
    practice.transfer_phone_number = "+15550009999"
    await db_session.commit()

    resp = await _transfer(client)
    assert resp.status_code == 200
    await asyncio.sleep(0)  # let the detached send task run
    assert len(paged) == 1
    assert paged[0]["to"] == "+15550009999"      # the clinic's own line, not the patient
    assert paged[0]["practice_name"] == "Paged Dental"


async def test_non_urgent_callback_does_not_page(client, db_session, monkeypatch):
    from app.webhooks import retell as retell_mod

    paged: list[dict] = []

    async def _fake_page(**kwargs):
        paged.append(kwargs)
        return {"sent": True}

    monkeypatch.setattr(retell_mod, "page_clinic_urgent_callback", _fake_page)
    await seed_practice(
        db_session, name="Quiet Dental", clerk_org_id="org_pg2", clerk_user_id="user_pg2"
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-call-2", "agent_id": "agent_tr2"},
            "name": "create_callback_request",
            "args": {"patient_first_name": "Ann", "patient_phone": "+15551110000",
                     "reason": "wants a price for whitening", "urgent": False},
        },
    )
    assert resp.status_code == 200
    await asyncio.sleep(0)
    assert paged == [], "routine callbacks must not text the clinic every time"


# ---------------------------------------------------------------------------
# TIER 1 — the emergency room, not our callback queue.
#
# "The team will call you back" is dangerous for an airway or a bleed that won't
# stop, and after hours it means "wait until morning". The tier is decided in the
# backend by regex over the tool arguments, so it holds regardless of what the
# model concluded and regardless of the hour.
# ---------------------------------------------------------------------------


async def test_life_threatening_signs_get_an_er_referral_not_a_callback_promise(
    client, db_session
):
    await seed_practice(
        db_session, name="ER One", clerk_org_id="org_er1", clerk_user_id="user_er1"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-er-1", "agent_id": "agent_er1"},
            "name": "create_callback_request",
            "args": {"patient_phone": "+15552223333", "urgent": False,
                     "reason": "my face is swelling and I can't breathe"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "er_referral"
    assert "911" in body["message"]
    # It must not tell the agent to offer a slot or a hold instead.
    assert "emergency room" in body["message"].lower()

    # The follow-up row is still written, and as urgent even though urgent=False
    # was passed — the caller described a life-threatening sign.
    await db_session.commit()
    db_session.expire_all()
    cb = (await db_session.execute(select(CallbackRequest))).scalars().all()
    assert len(cb) == 1 and cb[0].urgent is True


async def test_escalation_with_life_threatening_signs_also_refers_to_the_er(
    client, db_session
):
    await seed_practice(
        db_session, name="ER Two", clerk_org_id="org_er2", clerk_user_id="user_er2"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-er-2", "agent_id": "agent_er2"},
            "name": "transfer_to_human",
            "args": {"reason": "bleeding won't stop after the extraction"},
        },
    )
    assert resp.json()["status"] == "er_referral"


async def test_urgent_dental_pain_stays_ours(client, db_session):
    """Severe pain and a knocked-out tooth are dental urgencies, not ER cases —
    sending every one of those to an ER would be its own failure."""
    await seed_practice(
        db_session, name="ER Three", clerk_org_id="org_er3", clerk_user_id="user_er3"
    )
    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-er-3", "agent_id": "agent_er3"},
            "name": "create_callback_request",
            "args": {"patient_phone": "+15554445555", "urgent": True,
                     "reason": "severe pain in a back molar, knocked out tooth"},
        },
    )
    assert resp.json()["status"] == "callback_logged"


async def test_booking_texts_the_clinic_without_the_reason(client, db_session, monkeypatch):
    """A booking the clinic never sees is a double-booking waiting to happen —
    but the reason for the visit stays out of the SMS."""
    from app.webhooks import retell as retell_mod

    sent: list[dict] = []

    async def _fake_page(**kwargs):
        sent.append(kwargs)
        return {"sent": True}

    monkeypatch.setattr(retell_mod, "page_clinic_new_booking", _fake_page)
    practice, _ = await seed_practice(
        db_session, name="Alerted Dental", clerk_org_id="org_ba1", clerk_user_id="user_ba1"
    )
    practice.phone_number = "+15551112222"
    await db_session.commit()

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-ba-1", "agent_id": "agent_ba1"},
            "name": "book_appointment",
            "args": {"patient_first_name": "Dana", "patient_last_name": "Reed",
                     "patient_phone": "+15553334444", "procedure": "cleaning",
                     "preferred_date": "2099-12-15", "preferred_time_window": "morning"},
        },
    )
    assert resp.json()["booked"] is True
    await asyncio.sleep(0)
    assert len(sent) == 1
    assert sent[0]["to"] == "+15551112222"
    assert sent[0]["first_name"] == "Dana"
    assert "procedure" not in sent[0], "the visit reason belongs in the dashboard"


async def test_clinic_can_turn_booking_alerts_off(client, db_session, monkeypatch):
    from app.webhooks import retell as retell_mod

    sent: list[dict] = []

    async def _fake_page(**kwargs):
        sent.append(kwargs)
        return {"sent": True}

    monkeypatch.setattr(retell_mod, "page_clinic_new_booking", _fake_page)
    practice, _ = await seed_practice(
        db_session, name="Quiet Alerts", clerk_org_id="org_ba2", clerk_user_id="user_ba2"
    )
    practice.phone_number = "+15559990000"
    practice.booking_alerts_enabled = False
    await db_session.commit()

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-ba-2", "agent_id": "agent_ba2"},
            "name": "book_appointment",
            "args": {"patient_first_name": "Sam", "patient_last_name": "Lee",
                     "patient_phone": "+15557778888", "procedure": "cleaning",
                     "preferred_date": "2099-12-16", "preferred_time_window": "morning"},
        },
    )
    assert resp.json()["booked"] is True
    await asyncio.sleep(0)
    assert sent == []


async def test_transfer_destination_is_published_to_the_agent(db_session):
    """The native transfer tools dial {{clinic_transfer_number}} — if we don't
    fill it, the agent transfers into an empty string."""
    from app.services.llm.dynamic_vars import build_dynamic_variables

    practice, _ = await seed_practice(
        db_session, name="Dest Dental", clerk_org_id="org_dst", clerk_user_id="user_dst"
    )
    practice.transfer_phone_number = "+15551234567"
    practice.phone_number = "+15559999999"
    variables = build_dynamic_variables(practice)
    assert variables["clinic_transfer_number"] == "+15551234567"

    # No dedicated transfer line → the main office line.
    practice.transfer_phone_number = None
    assert build_dynamic_variables(practice)["clinic_transfer_number"] == "+15559999999"

    # No line at all → empty, so the prompt keeps the agent on the callback path.
    practice.phone_number = None
    assert build_dynamic_variables(practice)["clinic_transfer_number"] == ""


async def test_a_backend_failure_mid_call_gives_the_agent_something_to_say(
    client, db_session, monkeypatch
):
    """A DB blip during a tool call used to become a 500, and what the caller heard
    was then Retell's business, not ours — the same dead air we just spent a week
    removing. The agent must get a speakable answer that claims nothing."""
    from app.webhooks import retell as retell_mod

    async def _boom(*_a, **_kw):
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(retell_mod, "_dispatch_function", _boom)
    await seed_practice(
        db_session, name="Blip Dental", clerk_org_id="org_blip", clerk_user_id="user_blip"
    )

    resp = await client.post(
        "/webhooks/retell",
        json={
            "call": {"call_id": "tr-blip-1", "agent_id": "agent_blip"},
            "name": "book_appointment",
            "args": {"patient_first_name": "Ann", "patient_phone": "+15551110000",
                     "procedure": "cleaning", "preferred_date": "2099-12-20",
                     "preferred_time_window": "morning"},
        },
    )
    assert resp.status_code == 200, "a 500 hands the caller experience to Retell"
    body = resp.json()
    assert body["error"] == "backend_unavailable"
    # Whatever it says, it must not imply the booking happened.
    assert "booked" not in body
    assert "call them right back" in body["message"]
