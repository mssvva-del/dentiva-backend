"""What the agent still cannot answer about this practice, and what that costs.

A clinic goes live with whatever its website happened to mention. Harborside's
had no clinicians, no cancellation policy and no parking, so its agent met real
patients answering "the team will confirm that" to questions the front desk
answers in four words — and the only way anyone found out was the owner reading
transcripts and asking us to type the missing facts in by hand.

The list below is the receptionist's job written down: every entry is something
patients ask on a normal day. Each one says what the caller hears while it is
missing, because "incomplete profile" is not a reason anybody acts on and "the
agent cannot tell callers which dentist they will see" is.

Used by onboarding (to ask), by the clinic's settings (to finish), and by our
admin (to see which practices are answering blind).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.practice import Practice


@dataclass(frozen=True)
class Gap:
    field: str
    question: str        # asked of the clinic, in their words
    consequence: str     # what the caller hears until it is answered
    blocking: bool       # true when the agent gets something WRONG, not just vague
    done: bool = False   # the clinic has answered it


def _kb(practice: Practice) -> dict[str, Any]:
    kb = practice.knowledge_base
    return kb if isinstance(kb, dict) else {}


def _has_rows(value: Any) -> bool:
    return isinstance(value, list) and len([v for v in value if v]) > 0


def knowledge_checklist(practice: Practice) -> list[Gap]:
    """Everything a receptionist has to know, answered or not, costly first.

    One list, shared by the clinic's setup wizard and our admin screen. Naming
    these in two places meant the two could disagree about what a practice still
    owed us — and the hand-written copy left the policies out entirely, so a
    clinic could look ready while its agent could not say what happens if you
    cancel late.
    """
    return _build(practice)


def knowledge_gaps(practice: Practice) -> list[Gap]:
    """Only what is still missing."""
    return [g for g in knowledge_checklist(practice) if not g.done]


def _build(practice: Practice) -> list[Gap]:
    kb = _kb(practice)
    policies = kb.get("policies") if isinstance(kb.get("policies"), dict) else {}
    hours = practice.business_hours if isinstance(practice.business_hours, dict) else {}

    return [
        # ── Blocking: without these the agent is wrong, not merely vague ─────
        Gap(
            field="business_hours",
            question="What days and hours is the practice open?",
            consequence=(
                "The agent cannot tell callers when you are open, and cannot "
                "offer a time — every caller becomes a message to ring back."
            ),
            blocking=True,
            done=any(hours.values()),
        ),
        Gap(
            field="transfer_phone_number",
            question="Which number should urgent callers be put through to?",
            consequence=(
                "A patient in pain who asks for a person cannot be transferred "
                "to anyone."
            ),
            blocking=True,
            done=bool(practice.transfer_phone_number or practice.phone_number),
        ),
        Gap(
            field="appointment_types",
            question=(
                "What appointments do you book, and how long is each? "
                "(cleaning, new patient exam, emergency, crown…)"
            ),
            consequence=(
                "Every visit is offered as a generic slot, so a 60-minute crown "
                "prep can be booked into half an hour."
            ),
            blocking=True,
            done=_has_rows(kb.get("appointment_types")),
        ),

        # ── Asked on a normal day ───────────────────────────────────────────
        Gap(
            field="providers",
            question="Which dentists and hygienists see patients here?",
            consequence=(
                'Asked "who will I be seeing?" the agent has no answer — and it '
                "will not invent a name."
            ),
            blocking=False,
            done=_has_rows(kb.get("providers")),
        ),
        Gap(
            field="insurances",
            question="Which insurance plans are you in-network with?",
            consequence=(
                "Every caller asking about their plan is told the team will "
                "check — the most common question on a dental front desk."
            ),
            blocking=False,
            done=_has_rows(kb.get("insurances")),
        ),
        Gap(
            field="policies.cancellation",
            question="How much notice do you need to cancel, and is there a fee?",
            consequence=(
                "Callers cancelling late are not told about a fee, so they hear "
                "about it after the fact."
            ),
            blocking=False,
            done=bool(policies.get("cancellation")),
        ),
        Gap(
            field="policies.new_patient",
            question=(
                "What should a new patient bring, and how early should they "
                "arrive?"
            ),
            consequence=(
                "New patients arrive without their insurance card or ID and the "
                "visit starts late."
            ),
            blocking=False,
            done=bool(policies.get("new_patient")),
        ),
        Gap(
            field="policies.late",
            question="How late can somebody be before you reschedule them?",
            consequence=(
                "A caller running late cannot be told whether to come or to "
                "rebook."
            ),
            blocking=False,
            done=bool(policies.get("late")),
        ),
        Gap(
            field="policies.parking",
            question="Where do patients park?",
            consequence=(
                "Callers asking where to leave the car are not told, and arrive "
                "late looking for a space."
            ),
            blocking=False,
            done=bool(policies.get("parking")),
        ),
        Gap(
            field="self_pay",
            question="Do you see patients without insurance?",
            consequence=(
                "Uninsured callers are left unsure whether you will see them at "
                "all."
            ),
            blocking=False,
            done=kb.get("self_pay") is not None,
        ),
    ]


def gap_summary(practice: Practice) -> dict[str, Any]:
    """The shape both the wizard and the admin card render."""
    gaps = knowledge_gaps(practice)
    return {
        "total": len(gaps),
        "blocking": len([g for g in gaps if g.blocking]),
        "gaps": [
            {
                "field": g.field,
                "question": g.question,
                "consequence": g.consequence,
                "blocking": g.blocking,
            }
            for g in gaps
        ],
    }
