"""Relay system-prompt builder — renders the STRUCTURED knowledge base."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.llm.prompt import build_system_prompt


def _practice(**kb):
    return SimpleNamespace(
        name="Riverside Dental",
        business_hours={"mon": "9-6", "sat": "9-1"},
        address="12 Main St",
        languages_enabled=["en", "es"],
        knowledge_base=kb or {},
    )


def test_prompt_renders_structured_knowledge_base():
    p = _practice(
        providers=[
            {"name": "Dr. Smith", "type": "general", "accepts_new": True},
            {"name": "Ana Ruiz", "type": "hygienist", "accepts_new": False},
        ],
        appointment_types=[{"name": "Cleaning", "minutes": 60}, {"name": "Crown"}],
        insurances=["Delta Dental", "Cigna"],
        self_pay=True,
        policies={"cancellation": "24h notice or $50", "parking": "free lot"},
        emergency={"action": "transfer", "on_call_number": "+15550001111"},
    )
    prompt = build_system_prompt(p)
    assert "Riverside Dental" in prompt
    assert "Dr. Smith (general, accepting new)" in prompt
    assert "Ana Ruiz (hygienist, not taking new)" in prompt
    assert "Cleaning (60m)" in prompt and "Crown" in prompt
    assert "Delta Dental" in prompt and "Cigna" in prompt
    assert "Self-pay patients welcome" in prompt
    assert "Cancellation: 24h notice" in prompt and "Parking: free lot" in prompt
    assert "transfer to the on-call line" in prompt
    assert "Never promise coverage" in prompt


def test_prompt_renders_current_offer():
    # The clinic's own promotion must reach the agent so it can mention it.
    p = _practice(current_offer="New patient exam + X-rays $99")
    prompt = build_system_prompt(p)
    assert "CURRENT OFFER" in prompt
    assert "New patient exam + X-rays $99" in prompt
    # blank/absent offer → no CURRENT OFFER line
    assert "CURRENT OFFER" not in build_system_prompt(_practice())


def test_prompt_tolerates_empty_and_partial_kb():
    # No KB → still a valid prompt, no crash.
    assert "Riverside Dental" in build_system_prompt(_practice())
    # Partial KB (only insurances) → renders just that section.
    p = _practice(insurances=["Aetna"])
    prompt = build_system_prompt(p)
    assert "Aetna" in prompt
    assert "PROVIDERS" not in prompt  # section absent when no providers


def test_kb_block_is_length_capped():
    # A huge KB must not blow up the prompt (latency + injection surface).
    p = _practice(providers=[{"name": "Dr. " + "X" * 200, "type": "general"}] * 12)
    prompt = build_system_prompt(p)
    assert len(prompt) < 3000  # bounded
