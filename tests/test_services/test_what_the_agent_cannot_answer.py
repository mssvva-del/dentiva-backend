"""A clinic goes live with whatever its website happened to mention.

Harborside's had no clinicians, no cancellation policy and no parking. Its agent
met real patients answering "the team will confirm that" to questions a front
desk answers in four words, and the only way anyone found out was the owner
reading transcripts and asking us to type the missing facts in by hand.

These tests pin the list of what a receptionist has to know, and that each entry
says what the CALLER hears while it is missing — an "incomplete profile" warning
is not a reason anybody acts on.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.knowledge_gaps import gap_summary, knowledge_gaps

FULL_KB = {
    "providers": [{"name": "Dr. Zimlensky", "type": "general"}],
    "appointment_types": [{"name": "Cleaning", "minutes": 45}],
    "insurances": ["Delta Dental"],
    "self_pay": True,
    "policies": {
        "cancellation": "24 hours or a $50 fee.",
        "new_patient": "Arrive 15 minutes early with ID and insurance card.",
        "late": "More than 15 minutes late may be rescheduled.",
        "parking": "Free lot behind the building.",
    },
}
HOURS = {"mon": {"open": "09:00", "close": "17:30"}, "tue": None, "wed": None,
         "thu": None, "fri": None, "sat": None, "sun": None}


def _practice(**over):
    base = dict(
        knowledge_base=FULL_KB,
        business_hours=HOURS,
        phone_number="+19782837200",
        transfer_phone_number="+17819560377",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_a_complete_practice_has_nothing_left_to_ask():
    assert knowledge_gaps(_practice()) == []
    assert gap_summary(_practice())["total"] == 0


def test_the_practice_that_went_live_half_blind():
    """Exactly Harborside on the day it took its first real calls: hours and
    visit types known, nobody named, no policies at all."""
    kb = {k: v for k, v in FULL_KB.items() if k in ("appointment_types",)}
    fields = {g.field for g in knowledge_gaps(_practice(knowledge_base=kb))}
    assert "providers" in fields
    assert "insurances" in fields
    assert "policies.cancellation" in fields
    assert "policies.parking" in fields
    assert "self_pay" in fields
    assert "business_hours" not in fields   # those it did have


def test_every_gap_says_what_the_caller_hears():
    """"Incomplete profile" is not a reason anybody acts on."""
    for gap in knowledge_gaps(_practice(knowledge_base={}, business_hours={})):
        assert "?" in gap.question, gap.field
        assert len(gap.consequence) > 30, gap.field
        # The consequence is about the person on the phone, not about a field.
        assert any(
            word in gap.consequence.lower()
            for word in ("caller", "patient", "agent", "visit", "callers")
        ), gap.consequence


def test_no_hours_blocks_because_the_agent_is_wrong_not_vague():
    gaps = {g.field: g for g in knowledge_gaps(_practice(business_hours={}))}
    assert gaps["business_hours"].blocking is True


def test_no_appointment_types_blocks():
    kb = {k: v for k, v in FULL_KB.items() if k != "appointment_types"}
    gaps = {g.field: g for g in knowledge_gaps(_practice(knowledge_base=kb))}
    assert gaps["appointment_types"].blocking is True
    assert "crown" in gaps["appointment_types"].consequence.lower()


def test_nobody_to_transfer_an_urgent_caller_to_blocks():
    p = _practice(phone_number=None, transfer_phone_number=None)
    gaps = {g.field: g for g in knowledge_gaps(p)}
    assert gaps["transfer_phone_number"].blocking is True


def test_a_missing_policy_is_worth_asking_but_does_not_block():
    kb = dict(FULL_KB, policies={"cancellation": None, "new_patient": None,
                                 "late": None, "parking": None})
    gaps = {g.field: g for g in knowledge_gaps(_practice(knowledge_base=kb))}
    assert gaps["policies.cancellation"].blocking is False
    assert gap_summary(_practice(knowledge_base=kb))["blocking"] == 0


def test_the_blocking_ones_come_first():
    """A clinic finishing setup should meet the answers that change behaviour
    before the ones that only add polish."""
    gaps = knowledge_gaps(_practice(knowledge_base={}, business_hours={}))
    blocking = [i for i, g in enumerate(gaps) if g.blocking]
    optional = [i for i, g in enumerate(gaps) if not g.blocking]
    assert max(blocking) < min(optional)


def test_junk_in_the_column_does_not_crash_the_summary():
    # knowledge_base is JSONB and has been None, {} and a decryption failure.
    for kb in (None, {}, [], "nonsense"):
        assert gap_summary(_practice(knowledge_base=kb))["total"] > 0
