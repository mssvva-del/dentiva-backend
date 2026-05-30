#!/usr/bin/env python3
"""Seed demo data for Dentiva dashboard demo.

Run from backend directory:
    cd dentiva-backend && python3 scripts/seed_demo.py

Or inside Docker:
    docker compose exec api python3 scripts/seed_demo.py

Idempotent: checks for existing data before inserting.
Uses the admin (owner) role to bypass RLS.
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — ensure project root is importable regardless of cwd
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env before importing app modules (config reads env on import)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# App imports (config is now populated from .env)
# ---------------------------------------------------------------------------
from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models.booking import Booking  # noqa: E402
from app.models.call import Call  # noqa: E402
from app.models.patient import Patient  # noqa: E402
from app.models.practice import Practice  # noqa: E402

# ---------------------------------------------------------------------------
# Seed constants
# ---------------------------------------------------------------------------
FIRST_NAMES = [
    "James", "Maria", "Robert", "Jennifer", "Michael",
    "Linda", "David", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones",
    "Garcia", "Miller", "Davis", "Wilson", "Martinez",
    "Anderson", "Taylor", "Thomas", "White", "Harris",
]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com"]

PROCEDURES = [
    ("Teeth Cleaning", 30),
    ("Cavity Filling", 20),
    ("Root Canal", 10),
    ("Crown Placement", 10),
    ("Teeth Whitening", 10),
    ("Wisdom Tooth Extraction", 8),
    ("Dental Exam", 7),
    ("Orthodontic Consultation", 5),
]
PROCEDURE_NAMES = [p for p, _ in PROCEDURES]
PROCEDURE_WEIGHTS = [w for _, w in PROCEDURES]

PROVIDERS = ["Dr. Sarah Chen", "Dr. Michael Torres", "Dr. Emily Park"]

CALL_INTENTS = [
    "scheduling_new", "scheduling_existing", "reschedule",
    "insurance_question", "general_faq", "emergency",
]
SENTIMENTS = ["positive", "neutral", "anxious", "frustrated"]
SENTIMENT_WEIGHTS = [0.50, 0.30, 0.15, 0.05]

EST = timezone(timedelta(hours=-5))  # EST (no DST adjustment needed for demo)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rand_phone() -> str:
    return f"+1555{random.randint(1000000, 9999999)}"


def rand_working_datetime(days_back_max: int = 30) -> datetime:
    """Random datetime in the past N days, 9am-6pm EST."""
    offset_days = random.randint(0, days_back_max)
    offset_hours = random.randint(9, 17)
    offset_minutes = random.choice([0, 15, 30, 45])
    dt = datetime.now(EST) - timedelta(days=offset_days)
    dt = dt.replace(hour=offset_hours, minute=offset_minutes, second=0, microsecond=0)
    return dt


def rand_future_datetime(days_max: int = 60) -> datetime:
    """Random datetime in the next N days, 9am-5pm EST."""
    offset_days = random.randint(1, days_max)
    offset_hours = random.randint(9, 16)
    offset_minutes = random.choice([0, 15, 30, 45])
    dt = datetime.now(EST) + timedelta(days=offset_days)
    dt = dt.replace(hour=offset_hours, minute=offset_minutes, second=0, microsecond=0)
    return dt


def weighted_choice(choices, weights):
    return random.choices(choices, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# Main seed logic
# ---------------------------------------------------------------------------

def seed(session: Session) -> None:
    # ------------------------------------------------------------------
    # 0. Idempotency guard — check for DEMO-prefixed calls
    # ------------------------------------------------------------------
    existing_demo_calls = session.execute(
        select(func.count()).select_from(Call).where(Call.retell_call_id.like("DEMO-%"))
    ).scalar_one()

    if existing_demo_calls >= 30:
        existing_bookings = session.execute(
            select(func.count()).select_from(Booking)
        ).scalar_one()
        print(
            f"Demo data already seeded "
            f"(found {existing_demo_calls} demo calls, {existing_bookings} bookings). Skipping."
        )
        return

    # ------------------------------------------------------------------
    # 1. Find or create the demo practice
    # ------------------------------------------------------------------
    practice = session.execute(select(Practice).limit(1)).scalars().first()
    if practice is None:
        print("No practice found — creating demo practice...")
        practice = Practice(
            clerk_org_id="demo_org_dentiva",
            name="Smile Dental of New Jersey",
            timezone="America/New_York",
            phone_number="+15551234567",
            pms_system="open_dental",
            languages_enabled=["en"],
            business_hours={
                "mon": {"open": "09:00", "close": "18:00"},
                "tue": {"open": "09:00", "close": "18:00"},
                "wed": {"open": "09:00", "close": "18:00"},
                "thu": {"open": "09:00", "close": "18:00"},
                "fri": {"open": "09:00", "close": "17:00"},
                "sat": None,
                "sun": None,
            },
        )
        session.add(practice)
        session.flush()
        print(f"  Created practice: {practice.name}")

    print(f"Seeding demo data for practice: {practice.name}")
    practice_id = practice.id
    practice_phone = practice.phone_number or "+15551234567"

    # ------------------------------------------------------------------
    # 2. Create 15 patients (idempotent via pms_external_id)
    # ------------------------------------------------------------------
    patients: list[Patient] = []
    created_patients = 0
    for i, (first, last) in enumerate(zip(FIRST_NAMES, LAST_NAMES)):
        pms_id = f"DEMO-{i:02d}"
        existing = session.execute(
            select(Patient).where(
                Patient.practice_id == practice_id,
                Patient.pms_external_id == pms_id,
            )
        ).scalars().first()
        if existing:
            patients.append(existing)
            continue

        domain = random.choice(EMAIL_DOMAINS)
        patient = Patient(
            practice_id=practice_id,
            pms_external_id=pms_id,
            first_name=first,
            last_name=last,
            phone=rand_phone(),
            email=f"{first.lower()}.{last.lower()}@{domain}",
        )
        session.add(patient)
        patients.append(patient)
        created_patients += 1

    session.flush()  # get IDs for patients just inserted
    print(f"  Created {created_patients} patients ({len(patients) - created_patients} already existed)")

    # ------------------------------------------------------------------
    # 3. Create 80 calls
    # ------------------------------------------------------------------
    calls_completed: list[Call] = []
    calls_all: list[Call] = []

    # Status distribution: 65% completed, 20% missed, 15% voicemail
    status_choices = (
        ["completed"] * 65 + ["missed"] * 20 + ["voicemail"] * 15
    )
    counts = {"completed": 0, "missed": 0, "voicemail": 0}

    for i in range(80):
        retell_id = f"DEMO-{uuid.uuid4()}"
        status = random.choice(status_choices)
        counts[status] += 1

        # 90% inbound, 10% outbound
        direction = "inbound" if random.random() < 0.90 else "outbound"

        # from_number: use a patient phone 60% of the time
        patient = random.choice(patients) if random.random() < 0.60 else None
        from_number = patient.phone if (patient and patient.phone) else rand_phone()

        started_at = rand_working_datetime(30)

        if status == "completed":
            duration = random.randint(90, 420)
        else:
            duration = random.randint(5, 25)

        ended_at = started_at + timedelta(seconds=duration)

        # outcome: 40% of completed = booked
        outcome = None
        call_intent = None
        patient_sentiment = None
        escalation_needed = None

        if status == "completed":
            outcome = "booked" if random.random() < 0.40 else "info_only"
            call_intent = random.choice(CALL_INTENTS)
            patient_sentiment = weighted_choice(SENTIMENTS, SENTIMENT_WEIGHTS)
            escalation_needed = call_intent == "emergency" and random.random() < 0.05

        call = Call(
            practice_id=practice_id,
            retell_call_id=retell_id,
            direction=direction,
            from_number=from_number,
            to_number=practice_phone,
            patient_id=patient.id if patient else None,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
            status=status,
            outcome=outcome,
            call_intent=call_intent,
            patient_sentiment=patient_sentiment,
            escalation_needed=escalation_needed,
            hipaa_compliant=True,
        )
        session.add(call)
        calls_all.append(call)
        if status == "completed" and outcome == "booked":
            calls_completed.append(call)

    session.flush()  # get call IDs

    print(
        f"  Created 80 calls "
        f"({counts['completed']} completed, {counts['missed']} missed, {counts['voicemail']} voicemail)"
    )

    # ------------------------------------------------------------------
    # 4. Create bookings
    # ------------------------------------------------------------------
    bookings_created = 0

    # 4a. Bookings from AI calls (up to 32, limited by calls with outcome=booked)
    booked_calls = calls_completed[:32]
    for call in booked_calls:
        # Pick a patient: prefer the one on the call, else random
        patient = None
        if call.patient_id:
            patient = next((p for p in patients if p.id == call.patient_id), None)
        if patient is None:
            patient = random.choice(patients)

        # Decide past vs future: ~40% historical, 60% future
        if random.random() < 0.40:
            appointment_at = call.started_at + timedelta(days=random.randint(1, 30))
            # If it's still in the future relative to call time but past now — mark completed
            status = "completed" if appointment_at < datetime.now(EST) else "confirmed"
        else:
            appointment_at = rand_future_datetime(60)
            status = "confirmed"

        procedure = weighted_choice(PROCEDURE_NAMES, PROCEDURE_WEIGHTS)
        provider = random.choice(PROVIDERS)
        duration = random.choice([30, 45, 60, 90])

        booking = Booking(
            practice_id=practice_id,
            patient_id=patient.id,
            source_call_id=call.id,
            appointment_at=appointment_at,
            duration_minutes=duration,
            procedure_type=procedure,
            provider_name=provider,
            status=status,
            source="ai_call",
        )
        session.add(booking)
        bookings_created += 1

    ai_bookings = bookings_created

    # 4b. 18 direct phone bookings (no call link)
    for _ in range(18):
        patient = random.choice(patients)

        if random.random() < 0.40:
            # Historical
            days_ago = random.randint(1, 30)
            appointment_at = datetime.now(EST) - timedelta(days=days_ago)
            appointment_at = appointment_at.replace(
                hour=random.randint(9, 16),
                minute=random.choice([0, 15, 30, 45]),
                second=0, microsecond=0,
            )
            status = "completed"
        else:
            appointment_at = rand_future_datetime(60)
            status = "confirmed"

        procedure = weighted_choice(PROCEDURE_NAMES, PROCEDURE_WEIGHTS)
        provider = random.choice(PROVIDERS)
        duration = random.choice([30, 45, 60, 90])

        booking = Booking(
            practice_id=practice_id,
            patient_id=patient.id,
            source_call_id=None,
            appointment_at=appointment_at,
            duration_minutes=duration,
            procedure_type=procedure,
            provider_name=provider,
            status=status,
            source="phone",
        )
        session.add(booking)
        bookings_created += 1

    phone_bookings = bookings_created - ai_bookings

    session.commit()
    print(
        f"  Created {bookings_created} bookings "
        f"({ai_bookings} from AI calls, {phone_bookings} from phone)"
    )
    print("Done! Dashboard should now show realistic data.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    settings = get_settings()
    db_url = settings.database_url_sync
    if not db_url:
        # Fallback: convert async URL to sync
        db_url = settings.database_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg2://"
        )

    engine = create_engine(db_url, echo=False, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            seed(session)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
