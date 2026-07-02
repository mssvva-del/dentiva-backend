---
name: deploy-railway
description: Deploy / configure the Dentovox backend on Railway (dev/staging). Use when pushing backend changes to the Railway environment, adding env vars, running migrations, or seeding.
---

# Deploy the backend (Railway)

The backend runs on Railway for dev/staging. **Prod/main is gated** — the
reactivation engine lives on `feat/reactivation` until a live-clinic check.

## Rules
- **Secrets are the owner's job.** Never enter API keys/tokens into Railway
  Variables yourself — give the exact var names + say where the values live
  (`_KEYS_PRIVATE.md`, outside all repos). See `_docs/RAILWAY_VARS.md`.
- Never print secret values to chat.

## Steps
1. **Push** the feature branch; Railway auto-builds the connected service/branch.
2. **Env vars** (Railway → service → Variables) — the owner adds any new ones.
   Common: `DATABASE_URL`, `ENCRYPTION_KEY`, `CLERK_*`, `RETELL_*`, `GROQ_API_KEY`,
   `NEXHEALTH_API_KEY/SUBDOMAIN/LOCATION_ID`, `SENTRY_DSN`. Feature flags default OFF:
   `REACTIVATION_ENABLED`, `ENABLE_LLM_RELAY`, `SMS_ENABLED`, `REMINDERS_ENABLED`.
3. **Migrations** run on release (or one-off): `alembic upgrade head`. If a new
   migration backfills, confirm it completed before the new code serves traffic.
4. **Seed** (fresh env only) via the seed runner — see `_docs/RAILWAY_SEED.md`.
5. **Verify**: `GET /health` returns ok; check Sentry has no new errors; smoke the
   changed endpoint.

## Flipping a gated feature on
Set its flag to `true` in Railway Variables and redeploy. For `ENABLE_LLM_RELAY`
and outbound voice/SMS, only do this deliberately per the live-loop plan — they
touch real money / real patients.
