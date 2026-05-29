# Dentiva Backend

FastAPI backend. Local prototype mode — runs via Docker on Mac.

## Quick start

```bash
cp .env.example .env
# Fill in CLERK keys, ANTHROPIC_API_KEY, OPENAI_API_KEY, generate ENCRYPTION_KEY

docker compose up -d postgres
# Postgres on localhost:5432

# Create virtual env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

# Run migrations
alembic upgrade head

# Start FastAPI
uvicorn app.main:app --reload --port 8000

# Test
curl http://localhost:8000/health
```

## For Claude Code

Read `CLAUDE.md` and `START_PROMPT.md` before doing anything.

## Architecture

See `../_docs/ARCHITECTURE.md`.

## API

See `../_shared/API_CONTRACT.md`.

## Tech stack

- Python 3.12 + FastAPI
- PostgreSQL 15 + SQLAlchemy 2 + Alembic
- Clerk for auth
- Mock PMS adapter (Iter 1) → Open Dental (Iter 2)
