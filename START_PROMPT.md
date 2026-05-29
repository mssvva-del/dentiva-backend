# Backend — Phase 1 Detailed Plan

This is your detailed Phase 1 spec. Read CLAUDE.md first.

**Weekend Mode: Run locally via Docker. No cloud deployment.**

## Phase 1 goal

Working FastAPI app that:
1. Boots locally via Docker Compose
2. Has Postgres with full schema (from `_shared/DATA_MODEL.md`)
3. Has Clerk JWT verification middleware
4. Has Mock Open Dental adapter (real integration in Iter 2)
5. Has `GET /health` and `GET /api/practice/me` endpoints

Estimate: 4-6 hours total.

## Step-by-step

### Step 1 — Project skeleton (45 min)

Create:
- `pyproject.toml` with deps: fastapi, uvicorn[standard], sqlalchemy[asyncio], alembic, asyncpg, psycopg2-binary, pydantic-settings, httpx, python-jose[cryptography], cryptography, pytest, pytest-asyncio, ruff, mypy
- `Dockerfile` (multi-stage, Python 3.12-slim)
- `docker-compose.yml`: postgres15 only (backend runs natively in venv for dev)
- `.env.example` with all required env vars
- `app/main.py` with `/health` endpoint
- `app/config.py` with `Settings(BaseSettings)`

Test: `docker compose up -d postgres && curl localhost:8000/health` returns `{"status":"ok"}`.

Commit: `feat: initial FastAPI skeleton with Docker Postgres`

### Step 2 — Database schema (60 min)

- Initialize Alembic: `alembic init migrations`
- Create SQLAlchemy 2.0 models for ALL tables in `_shared/DATA_MODEL.md`:
  - practices, users, patients, calls, bookings, audit_logs
  - (skip `outreach` — defer to Phase 2 when Telnyx is set up)
- Generate initial migration: `alembic revision --autogenerate -m "initial_schema"`
- Verify migration file (column types, constraints, indexes)
- Apply: `alembic upgrade head`
- Verify with `docker exec`: `\dt` shows all 6 tables

Note: encrypted columns (first_name, last_name, phone, email, dob on `patients`) use `bytea` type — encryption logic in Step 5.

Commit: `feat: database schema and initial migration`

### Step 3 — Clerk auth middleware (45 min)

- Create `app/middleware/auth.py`
- JWKS verification of Clerk JWT (cache JWKS 1 hour)
- Extract `clerk_user_id`, `clerk_org_id` from token claims
- SQLAlchemy session context: set `app.current_practice_id`
- Create `app/dependencies.py` with `get_current_user()` and `get_current_practice()`

Test: pytest with mock JWT — no token → 401, bad token → 401, valid token → 200.

Commit: `feat: Clerk JWT auth middleware`

### Step 4 — First endpoint: GET /api/practice/me (30 min)

- Create `app/routes/practice.py`
- Implement `GET /api/practice/me` per `_shared/API_CONTRACT.md`
- Pydantic response schema in `app/schemas/practice.py`
- Wire into `main.py`
- Integration test with seeded practice + mock Clerk JWT

Commit: `feat: GET /api/practice/me endpoint`

### Step 5 — Mock Open Dental adapter (60 min)

**Weekend mode**: build adapter interface + MOCK implementation. Real integration with Open Dental API is Iter 2.

- Create `app/adapters/open_dental/`
  - `interface.py` — abstract `PMSAdapter` with methods: `get_patient(id)`, `check_availability(date_range)`, `create_appointment(...)`, `get_patient_by_phone(phone)`
  - `mock.py` — `MockOpenDentalAdapter` returning realistic fake data
  - `client.py` — empty stub for real implementation later
  - `models.py` — Pydantic models matching expected Open Dental responses
- Use mock adapter throughout codebase (env var `PMS_ADAPTER=mock` vs `PMS_ADAPTER=open_dental_real`)
- Unit tests for mock adapter

Commit: `feat: Mock Open Dental adapter with interface`

### Step 6 — Encryption helper (45 min)

- Create `app/utils/crypto.py`
- Use `cryptography.fernet`
- Key from env var `ENCRYPTION_KEY`
- In `.env.example` include: `# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- Helper functions: `encrypt_pii(str) -> bytes`, `decrypt_pii(bytes) -> str`
- Custom SQLAlchemy `TypeDecorator` for `EncryptedString` columns
- Apply to patient model
- Test: round-trip + tampering detection

Commit: `feat: PII encryption with Fernet`

### Step 7 — Test infrastructure (30 min)

- `tests/conftest.py`: async DB fixture, app fixture, auth fixture, mock practice
- `tests/test_routes/test_practice.py`: covers Step 4
- `tests/test_adapters/test_open_dental_mock.py`: covers Step 5
- `tests/test_utils/test_crypto.py`: covers Step 6
- Run `pytest -v` — all green

Commit: `test: Phase 1 test coverage`

### Step 8 — Multi-tenancy isolation test (45 min)

This is the IMPORTANT test that prevents data leaks.

- Seed 2 practices: A and B
- Each gets 1 patient
- User of practice A queries `/api/calls` — gets only A's data
- User of practice A queries `/api/patients/<B-patient-id>` → 404 (not 403, avoid info leakage)
- Add PostgreSQL Row-Level Security policy on `patients` table
- Test: even if Python code forgets `WHERE practice_id = ...`, DB blocks it

Commit: `feat: multi-tenant isolation with RLS`

### Step 9 — Wrap up Phase 1 (15 min)

- Update `/Users/sergmols/Projects/dentiva-starter/_docs/PROGRESS.md` with what's done
- Make `README.md` has clear local-dev instructions
- Run all acceptance criteria from CLAUDE.md
- Notify user: "Phase 1 complete. Ready for Phase 2 (Retell webhook + booking flow). Please review."

## Acceptance criteria checklist

Before marking Phase 1 done:
- [ ] `docker compose up -d postgres` works
- [ ] `python -m app.main` starts FastAPI on port 8000
- [ ] `pytest -v` → all green
- [ ] `ruff check .` → clean
- [ ] `mypy app/` → clean
- [ ] `curl localhost:8000/health` returns 200
- [ ] Clerk-protected endpoint returns 401 without auth
- [ ] All 6 tables exist in DB after migration
- [ ] Multi-tenant isolation test passes
- [ ] `_docs/PROGRESS.md` updated
- [ ] Git: clean working tree, all commits pushed

If ANY of these fail, Phase 1 is not done.

## Common pitfalls

1. **SQLAlchemy async**: use `AsyncSession`, not `Session`. Don't mix.
2. **Alembic + async**: Alembic itself runs sync. Use `psycopg2-binary` in `alembic/env.py`, `asyncpg` in app.
3. **Pydantic v2**: `model_validate` not `parse_obj`. `model_dump` not `dict()`.
4. **Clerk JWT**: `iss` claim includes Clerk frontend host — verify match.
5. **Docker Mac**: M2 needs `platform: linux/arm64` for some images or just let Docker pick.

## When you finish

Don't auto-start Phase 2. Wait for user explicit "go".

Phase 2 preview: Retell webhook handler with idempotency, booking creation flow.
