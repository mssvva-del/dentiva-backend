"""Custom clinic-authored campaigns: contact intake, campaign CRUD, TCPA gates."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.reactivation import ReactivationCampaign, ReactivationTarget
from app.services.reactivation.contacts import parse_contacts
from tests.conftest import seed_practice


def _h(org, user):
    return {"X-Dev-Clerk-User-Id": user, "X-Dev-Clerk-Org-Id": org}


# ── parser (pure) ────────────────────────────────────────────────────────────
def test_parse_single_number():
    rows, rep = parse_contacts("732-284-0545")
    assert len(rows) == 1 and rows[0].phone == "+17322840545"
    assert rep.invalid == []


def test_parse_pasted_list_and_country_code():
    rows, rep = parse_contacts("17322840545\n+1 (917) 572-2233\n9175722233")
    assert [r.phone for r in rows] == ["+17322840545", "+19175722233"]
    assert rep.duplicates_in_file == 1  # 917 повторился


def test_parse_csv_with_headers():
    csv_text = ("first_name,last_name,phone,language\n"
                "Mike,Vinnik,7322840545,en\nAna,Lopez,9175722233,es\n")
    rows, rep = parse_contacts(csv_text)
    assert rows[0].first_name == "Mike" and rows[0].last_name == "Vinnik"
    assert rows[1].language == "es"
    assert rep.invalid == []


def test_parse_headerless_name_phone():
    rows, _ = parse_contacts("Mike,7322840545\nAna Lopez,9175722233")
    assert rows[0].first_name == "Mike" and rows[0].phone == "+17322840545"


def test_parse_rejects_garbage():
    rows, rep = parse_contacts("hello\n123\n555-01")  # ни одного валидного
    assert rows == [] and len(rep.invalid) == 3


# ── upload endpoint ──────────────────────────────────────────────────────────
async def test_upload_contacts_upserts(client, db_session):
    await seed_practice(db_session, name="UpCo", clerk_org_id="org_up1",
                        clerk_user_id="user_up1")
    h = _h("org_up1", "user_up1")
    r = await client.post("/api/reactivation/contacts", headers=h,
                          json={"contacts_text": "Mike,7322840545\n9175722233"})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 2 and body["updated"] == 0
    # Re-upload → update, not duplicate.
    r2 = await client.post("/api/reactivation/contacts", headers=h,
                           json={"contacts_text": "7322840545"})
    assert r2.json()["updated"] == 1 and r2.json()["added"] == 0


async def test_upload_all_garbage_422(client, db_session):
    await seed_practice(db_session, name="UpCo2", clerk_org_id="org_up2",
                        clerk_user_id="user_up2")
    r = await client.post("/api/reactivation/contacts", headers=_h("org_up2", "user_up2"),
                          json={"contacts_text": "not a phone"})
    assert r.status_code == 422


# ── campaign create/launch ───────────────────────────────────────────────────
async def test_create_treatment_campaign_and_launch(client, db_session):
    practice, _ = await seed_practice(db_session, name="CampCo",
                                      clerk_org_id="org_cc1", clerk_user_id="user_cc1")
    h = _h("org_cc1", "user_cc1")
    r = await client.post("/api/reactivation/campaigns", headers=h, json={
        "name": "Annual checkup push",
        "contacts_text": "Mike,7322840545\nAna,9175722233",
        "custom_context": "It's time for your annual checkup with Dr. Chen.",
        "category": "treatment", "channels": "sms", "launch_now": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["campaign"]["status"] == "running"
    assert body["campaign"]["enrolled"] == 2
    assert body["intake"]["added"] == 2

    lst = await client.get("/api/reactivation/campaigns", headers=h)
    assert lst.status_code == 200 and len(lst.json()) == 1

    # pause → launch again
    cid = body["campaign"]["id"]
    p = await client.post(f"/api/reactivation/campaigns/{cid}/pause", headers=h)
    assert p.status_code == 200 and p.json()["status"] == "paused"
    l2 = await client.post(f"/api/reactivation/campaigns/{cid}/launch", headers=h)
    assert l2.status_code == 200 and l2.json()["status"] == "running"


async def test_marketing_requires_attestation(client, db_session):
    await seed_practice(db_session, name="CampCo2", clerk_org_id="org_cc2",
                        clerk_user_id="user_cc2")
    h = _h("org_cc2", "user_cc2")
    base = {
        "name": "Anniversary offer",
        "contacts_text": "7322840545",
        "custom_context": "Celebrate our anniversary — 20% off whitening!",
        "category": "marketing", "channels": "sms",
    }
    # без галочки → 422
    r = await client.post("/api/reactivation/campaigns", headers=h,
                          json={**base, "consent_attested": False})
    assert r.status_code == 422
    # с галочкой → ок, attestation записана
    r2 = await client.post("/api/reactivation/campaigns", headers=h,
                           json={**base, "consent_attested": True})
    assert r2.status_code == 200
    c = (await db_session.execute(select(ReactivationCampaign))).scalars().first()
    assert c.category == "marketing" and c.consent_attested_by is not None
    assert c.consent_attested_at is not None


async def test_campaign_rbac_and_isolation(client, db_session):
    from app.models.user import User
    p1, _ = await seed_practice(db_session, name="IsoA", clerk_org_id="org_iso_a",
                                clerk_user_id="user_iso_a")
    await seed_practice(db_session, name="IsoB", clerk_org_id="org_iso_b",
                        clerk_user_id="user_iso_b")
    # viewer в клинике A не может (нет MANAGE_SETTINGS)
    db_session.add(User(id=uuid.uuid4(), clerk_user_id="viewer_iso",
                        practice_id=p1.id, email="v@iso.com", role="viewer"))
    await db_session.commit()
    denied = await client.post("/api/reactivation/contacts",
                               headers=_h("org_iso_a", "viewer_iso"),
                               json={"contacts_text": "7322840545"})
    assert denied.status_code == 403
    # без auth → 401
    anon = await client.get("/api/reactivation/campaigns")
    assert anon.status_code == 401

    # клиника A создаёт кампанию; клиника B её не видит
    await client.post("/api/reactivation/campaigns", headers=_h("org_iso_a", "user_iso_a"),
                      json={"name": "A camp", "contacts_text": "7322840545",
                            "category": "treatment", "channels": "sms"})
    b_list = await client.get("/api/reactivation/campaigns",
                              headers=_h("org_iso_b", "user_iso_b"))
    assert b_list.status_code == 200 and b_list.json() == []


# ── worker integration: custom body + promo gate ─────────────────────────────
async def test_worker_sends_custom_text_and_gates_promo(client, db_session, monkeypatch):
    """Treatment campaign with clinic text → SMS contains the text.
    Unattested promo text in a treatment campaign → blocked by the gate.
    Attested marketing → promo text passes."""
    from app.services.reactivation import outreach as outreach_mod
    from app.services.reactivation.outreach import process_due_targets
    from app.services.reactivation.scheduling import CadenceStep, CampaignConfig

    practice, owner = await seed_practice(db_session, name="WrkCo",
                                          clerk_org_id="org_wrk", clerk_user_id="user_wrk")
    h = _h("org_wrk", "user_wrk")
    sent_bodies: list[str] = []

    async def _fake_send(phone, body, *, opted_out=False, client=None):  # noqa: ANN001
        sent_bodies.append(body)
        return {"sent": True, "sid": f"SM{len(sent_bodies)}"}
    monkeypatch.setattr(outreach_mod, "send_sms", _fake_send)
    cfg = CampaignConfig(cadence=(CadenceStep("sms", 0),),
                         quiet_start_hour=0, quiet_end_hour=24)

    async def _make_due():
        """Тест не зависит от юр-окна 9-20: касания принудительно 'пора'."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update
        await db_session.execute(update(ReactivationTarget).values(
            next_touch_at=datetime.now(tz=UTC) - timedelta(minutes=1)))
        await db_session.commit()


    # 1) treatment + нормальный текст → уходит с текстом клиники
    await client.post("/api/reactivation/campaigns", headers=h, json={
        "name": "Checkup", "contacts_text": "Mike,7322840545",
        "custom_context": "Dr. Chen has openings this week for your checkup.",
        "category": "treatment", "channels": "sms", "launch_now": True,
    })
    await _make_due()
    await process_due_targets(db_session, practice.id, config=cfg)
    assert any("Dr. Chen has openings" in b for b in sent_bodies)
    assert all("STOP" in b for b in sent_bodies)  # opt-out футер всегда

    # 2) treatment + промо-слова → контент-гейт блокирует отправку
    sent_bodies.clear()
    await client.post("/api/reactivation/campaigns", headers=h, json={
        "name": "Sneaky promo", "contacts_text": "9175722233",
        "custom_context": "Get 20% off whitening this month!",
        "category": "treatment", "channels": "sms", "launch_now": True,
    })
    await _make_due()
    await process_due_targets(db_session, practice.id, config=cfg)
    assert sent_bodies == []  # заблокировано, ничего не ушло

    # 3) marketing + attestation → промо-текст уходит
    sent_bodies.clear()
    await client.post("/api/reactivation/campaigns", headers=h, json={
        "name": "Anniversary", "contacts_text": "9175722234",
        "custom_context": "Anniversary special — 20% off whitening!",
        "category": "marketing", "channels": "sms",
        "consent_attested": True, "launch_now": True,
    })
    await _make_due()
    await process_due_targets(db_session, practice.id, config=cfg)
    assert any("20% off whitening" in b for b in sent_bodies)

    # у промо-таргета из шага 2 касание не потеряно навсегда — оно skipped
    skipped = (await db_session.execute(
        select(ReactivationTarget).where(ReactivationTarget.practice_id == practice.id)
    )).scalars().all()
    assert len(skipped) == 3


async def test_voice_touch_gates_unattested_promo_context(db_session):
    """Reviewer: the voice channel must apply the SAME promo gate as SMS —
    an unattested promo context never reaches the dialer."""
    import httpx

    from app.services.reactivation.voice import RetellOutboundClient, make_voice_touch

    practice, _ = await seed_practice(db_session, name="VGate",
                                      clerk_org_id="org_vg", clerk_user_id="user_vg")
    from app.models.patient import Patient
    from app.models.reactivation import ReactivationCampaign as C
    from app.models.reactivation import ReactivationTarget as T
    patient = Patient(id=uuid.uuid4(), practice_id=practice.id,
                      pms_external_id="x-vg", first_name="V", phone="+17322840545")
    camp = C(id=uuid.uuid4(), practice_id=practice.id, name="promo",
             segment="custom", status="running", category="treatment",
             custom_context="20% off whitening!", channels="voice")
    db_session.add_all([patient, camp])
    await db_session.flush()
    target = T(id=uuid.uuid4(), practice_id=practice.id, campaign_id=camp.id,
               patient_id=patient.id, segment="custom", status="pending")
    db_session.add(target)
    await db_session.commit()

    dialed = []

    def handler(req: httpx.Request) -> httpx.Response:
        dialed.append(req.url.path)
        return httpx.Response(200, json={"call_id": "c1"})

    client = RetellOutboundClient(api_key="k", from_number="+16204559562",
                                  transport=httpx.MockTransport(handler))
    res = await make_voice_touch(patient, "en", target, client=client,
                                 campaign=camp, practice_name="VGate")
    assert res.status == "skipped" and res.outcome == "content_blocked"
    assert dialed == []  # номер НЕ набирался

    # attested marketing → звонок уходит
    camp.category = "marketing"
    camp.consent_attested_by = uuid.uuid4()
    res2 = await make_voice_touch(patient, "en", target, client=client,
                                  campaign=camp, practice_name="VGate")
    assert res2.status == "sent" and len(dialed) == 1
