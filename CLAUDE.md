# Dentovox Backend — Engineering Guide

FastAPI backend for Dentovox: an AI voice receptionist + patient reactivation engine
for US dental practices. Multi-tenant, HIPAA-minded. Scope = this folder.

## Stack
- Python 3.12, FastAPI, Pydantic v2 settings
- SQLAlchemy 2 (async, asyncpg) + Alembic, PostgreSQL 15
- Clerk JWT auth · Retell (voice) · Groq (LLM) · Twilio (SMS) · NexHealth/Open Dental (PMS)
- pytest (async) · ruff (lint) · Sentry (PHI-scrubbed)

## Commands
```bash
docker compose up -d db                       # Postgres on :5432
export DATABASE_URL="postgresql+asyncpg://dentiva:dentiva@localhost:5432/dentiva"
.venv/bin/alembic upgrade head                # apply migrations
.venv/bin/python -m pytest -q                 # full suite (needs db up)
.venv/bin/ruff check app/ tests/              # lint (must be clean)
```
Migrations run as superuser `dentiva`; the app connects as `dentiva_app` (NOBYPASSRLS).

## Layout
```
app/
  main.py            FastAPI app: lifespan (background loops), router mounts, gated features
  config.py          Pydantic Settings (all env flags)
  db.py              async engine, Base, set_tenant() — RLS tenant context
  models/            SQLAlchemy models (one per table)
  schemas/           Pydantic request/response
  routes/            HTTP routers (/api/*)
  webhooks/          retell.py (voice tools), twilio_sms.py, clerk.py, stripe.py
  services/          business logic — reactivation/ (the engine), sms, booking, llm/
  adapters/          PMS clients — nexhealth/, open_dental/ (+ mocks)
  api/llm_relay.py   Retell custom-LLM WebSocket → Groq (gated, ENABLE_LLM_RELAY)
  utils/crypto.py    Fernet PII encrypt/decrypt + phone_hmac (searchable hash)
migrations/          Alembic revisions
tests/               test_routes/ test_services/ test_adapters/ (conftest builds schema + RLS)
scripts/             one-off ops (backfill_phone_hmac, seeds)
```

## Conventions (non-negotiable)
- **Multi-tenancy = Postgres RLS.** Every PHI query runs after `await set_tenant(session, practice_id)`.
  Tables carry FORCE RLS; the app role can't bypass it. New PHI table → add to the RLS list
  (conftest + scripts/check_rls_coverage.py) or the gate fails.
- **No PHI in logs.** Never log names/phone/DOB/transcript. Log `patient_id`, last-4, counts.
- **PII columns** use `EncryptedString` (Fernet bytea). Phone is also mirrored to the
  deterministic, indexed `patients.phone_hmac` — look patients up by that, never by scanning.
- **Webhooks are idempotent** (retell_call_id / event id dedup). Writes are never auto-retried
  (double-book risk); external calls go through `utils/resilience` (timeout + retry on reads).
- **Schema changes need a migration** (up + down, tested `alembic upgrade/downgrade`).
- **Every endpoint** needs happy-path + auth-failure + tenant-isolation tests. `ruff` clean.
- **Branch + PR.** Do NOT merge to `main` — `feat/reactivation` is the integration branch;
  main is gated on a live-clinic check (audit RISK 1). Commit style: `feat:`/`fix:`/`perf:`/`chore:`.

## Key entry points
- **Voice call** → `webhooks/retell.py`: function-call router (book/reschedule/cancel/waitlist/
  transfer) + a PROGRAMMATIC emergency lock (persisted in `calls.emergency_active`, not prompt-based).
- **Reactivation engine** → `services/reactivation/`: NexHealth pull → segmentation → scoring →
  scheduler → outreach (SMS+voice) → PMS write-back → ROI. Worker loop in `worker.py`, gated by
  `REACTIVATION_ENABLED` (off by default — never auto-dials the demo).
- **Groq relay** → `api/llm_relay.py`: runs the receptionist on our Groq loop; gated
  `ENABLE_LLM_RELAY` (off — demo uses Retell-managed model). Conversational only; tools stay on
  the signed webhook.

## Gated flags (default OFF — flip deliberately)
`REACTIVATION_ENABLED`, `ENABLE_LLM_RELAY`, `SMS_ENABLED`, `REMINDERS_ENABLED`.

## When unsure (<80% confident)
STOP. Write the question to `_docs/QUESTIONS.md` and tell the user. Don't guess or "try and iterate".

## Anti-patterns (do not repeat)
Refactor without a test · code that "should work" un-run · schema change without migration ·
auth logic inside routes (belongs in deps/middleware) · silent `except:` · PHI in logs ·
scanning+decrypting to find a patient (use `phone_hmac`) · installing packages "just in case".
