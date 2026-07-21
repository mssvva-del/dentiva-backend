"""Phase 1 webhook tests — kept as-is for regression coverage.

Note: these tests use the `client` fixture which ensures a fresh DB engine,
so each test gets a clean connection pool via _prepare_database.
"""

from tests.conftest import seed_practice


async def test_book_appointment_function_call(client, db_session):
    # No practice seeded — nothing to compute availability against, so the webhook
    # returns an empty slot list (never invents times) and does not persist.
    payload = {
        "event": "function_call",
        "call_id": "retell_call_xyz",
        "function_name": "book_appointment",
        "args": {
            "patient_first_name": "Maria",
            "patient_last_name": "Garcia",
            "patient_phone": "+15551234567",
            "procedure": "cleaning",
            "preferred_date": "2026-06-05",
            "preferred_time_window": "morning",
        },
    }
    resp = await client.post("/webhooks/retell", json=payload)
    assert resp.status_code == 200
    assert resp.json()["result"]["available_slots"] == []


async def test_call_started_ack(client, db_session):
    # Seeds a practice so the call_started can persist the call row.
    await seed_practice(
        db_session, name="Ack Dental", clerk_org_id="org_ack", clerk_user_id="user_ack"
    )
    resp = await client.post(
        "/webhooks/retell", json={"event": "call_started", "call_id": "c1"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
