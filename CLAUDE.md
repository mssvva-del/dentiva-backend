# Dentiva Backend — Working Agreement

You are the backend engineer for Dentiva. Your scope is this folder only.

**WEEKEND MODE: Build to run locally on Sergio's M2 Mac via Docker. No AWS. No cloud deployments. Use free tier services only.**

## Stack

- Python 3.12, FastAPI, Pydantic v2
- PostgreSQL 15 via SQLAlchemy 2 + Alembic
- Pytest for tests
- Deployment (later): AWS Lambda + API Gateway
- Local dev: Docker Compose (Postgres only — Mac has Docker Desktop)

## Mandatory first read

Before ANY work, view these files:
1. `/Users/sergmols/Projects/dentiva-starter/_docs/ARCHITECTURE.md`
2. `/Users/sergmols/Projects/dentiva-starter/_shared/API_CONTRACT.md`
3. `/Users/sergmols/Projects/dentiva-starter/_shared/DATA_MODEL.md`
4. `./START_PROMPT.md`

Then say "Ready for Phase 1" — do not start coding yet.

## Hard rules

1. **Scope**: only endpoints listed in `API_CONTRACT.md` for Iter 1. Out of scope = STOP.
2. **No PHI in logs**: never log patient names, phone, DOB. Use `patient_id` references.
3. **Multi-tenancy everywhere**: every query touching PHI filters by `practice_id`. SQLAlchemy session-level filter. Test for missing filters.
4. **Idempotency**: webhook handlers idempotent via `retell_call_id` dedup.
5. **Secrets**: never hardcode. Use `.env` locally (gitignored).
6. **No deploy without tests**: every endpoint needs happy-path + auth failure + multi-tenant isolation tests.
7. **Weekend Mode**: NO AWS calls. NO production secrets. Mock external services when convenient.

## When stuck

If <80% confident:
- STOP coding
- Write question to `/Users/sergmols/Projects/dentiva-starter/_docs/QUESTIONS.md`
- Tell user "I have a question in _docs/QUESTIONS.md"

Do NOT guess. Do NOT "try and iterate" — past CRM bugs came from this pattern.

## Phase structure

Each phase ends with:
- All new tests pass: `pytest -v`
- Lint clean: `ruff check .` and `mypy app/`
- Docker compose starts: `docker compose up -d && curl localhost:8000/health`
- Git commit with conventional message: `feat:`, `fix:`, `chore:`
- Update `/Users/sergmols/Projects/dentiva-starter/_docs/PROGRESS.md`

If ANY criterion fails — Phase NOT done.

## Folder layout

```
dentiva-backend/
├── app/
│   ├── main.py              # FastAPI entry
│   ├── config.py            # Pydantic settings
│   ├── db.py                # SQLAlchemy session
│   ├── models/              # SQLAlchemy models (one per table)
│   ├── schemas/             # Pydantic request/response
│   ├── routes/              # FastAPI routers
│   ├── services/            # Business logic
│   ├── webhooks/            # Webhook handlers
│   ├── adapters/            # External APIs (Open Dental, Retell)
│   ├── middleware/          # Auth, tenant context, audit
│   └── utils/               # Encryption, formatting
├── migrations/              # Alembic
├── tests/
│   ├── conftest.py
│   ├── test_routes/
│   ├── test_services/
│   └── test_adapters/
├── docker-compose.yml       # Postgres only — backend runs natively
├── Dockerfile               # For later AWS deployment
├── pyproject.toml
├── alembic.ini
├── .env.example
└── CLAUDE.md (this file)
```

## Key patterns

### Multi-tenant query helper
```python
# app/db.py
def tenant_query(model, practice_id: UUID):
    return select(model).where(model.practice_id == practice_id)
```

### Webhook idempotency
```python
async def handle_retell_webhook(payload: dict, db: AsyncSession):
    if await call_already_processed(payload['call_id'], db):
        return {"ok": True, "deduplicated": True}
    # process...
```

### Encrypted PII
```python
# app/utils/crypto.py
def encrypt_pii(value: str) -> bytes
def decrypt_pii(blob: bytes) -> str

# In model:
class Patient(Base):
    first_name = Column(EncryptedString)
```

## Phase 1 acceptance (high level)

By end of Phase 1:
- FastAPI boots, `/health` returns `{"status":"ok"}`
- All tables from `DATA_MODEL.md` exist via Alembic migration
- `GET /api/practice/me` works with Clerk JWT
- Mock Open Dental adapter (returns fake patient — real integration later)
- Tests cover all above
- Multi-tenant isolation test passes

Phase 2 = Retell webhook + booking flow.
Phase 3 = Hardening + audit logs.

Read `START_PROMPT.md` for detailed steps.

## Things to ask user about (in QUESTIONS.md)

If user hasn't given you these, ask:

1. Clerk Publishable + Secret keys
2. Retell test mode webhook signing secret (will create later)
3. Database encryption key (or "generate one for me")

For Open Dental — use MOCK adapter on weekend (real integration is Phase 2 of Iter 2).

## When user says "next phase"

Don't auto-start. Re-read CLAUDE.md and START_PROMPT.md, summarize next steps in 3 bullets, wait for "go".

## Token economy

- Don't re-read same file twice in one session — remember contents
- Don't paste large files in responses; reference paths
- Use `grep` and `view` with `view_range` instead of full reads
- Run tests with `-x` (stop at first failure)
- If directory has >20 files, ask before recursive view

## Anti-patterns (from past projects — DO NOT repeat)

- ❌ "Let me try refactoring" without writing test first
- ❌ Code that "should work" without running it
- ❌ DB schema changes without migration file
- ❌ Auth logic inside routes (goes in middleware)
- ❌ Catching all exceptions silently
- ❌ Different error shapes from different endpoints
- ❌ Installing packages "just in case"
