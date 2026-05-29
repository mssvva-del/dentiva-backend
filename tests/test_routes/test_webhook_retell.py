async def test_book_appointment_function_call(client):
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
    slots = resp.json()["result"]["available_slots"]
    assert len(slots) == 3
    assert slots[0]["date"] == "2026-06-05"
    assert {"date", "time", "provider"} <= set(slots[0].keys())


async def test_call_started_ack(client):
    resp = await client.post(
        "/webhooks/retell", json={"event": "call_started", "call_id": "c1"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
