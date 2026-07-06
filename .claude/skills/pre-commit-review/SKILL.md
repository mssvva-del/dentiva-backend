---
name: pre-commit-review
description: Self-review checklist before committing backend changes. Use right before any git commit / opening a PR to catch tenant leaks, PHI logging, missing tests/migrations, and secrets.
---

# Pre-commit review

Run through this before `git commit`. Stop and fix on any ❌.

## Correctness & tests
- [ ] `.venv/bin/python -m pytest -q` green (db up). New behavior has tests:
      happy path + auth failure + **tenant isolation**.
- [ ] `ruff check app/ tests/` clean.
- [ ] Schema changed? A migration exists and `alembic upgrade/downgrade/upgrade`
      round-trips on real Postgres. One `alembic heads`.

## Security / tenancy (HIPAA)
- [ ] Every PHI query runs after `set_tenant(...)` / uses the tenant-bound session.
- [ ] New PHI table added to the RLS list (conftest + check_rls_coverage.py) + FORCE RLS.
- [ ] **No PHI in logs** — grep your diff for name/phone/dob/transcript in log calls.
- [ ] Patient lookups use `phone_hmac`, not a decrypt-scan.
- [ ] No secret / `.env` staged (`git diff --cached`). If a secret ever landed in a
      commit, it must be **ROTATED**, not just removed (history keeps it).

## Scope & hygiene
- [ ] Writes are idempotent / not auto-retried where a retry could double-book.
- [ ] Feature branch, NOT `main` (main is gated on the live-clinic check).
- [ ] No junk files (`*.tmp`, dumps, `.DS_Store`) or debug scripts left in the tree.
- [ ] Commit message: `feat:`/`fix:`/`perf:`/`chore:` + a one-line why.

## For risky changes (voice hot path, migrations, auth)
Consider an independent adversarial review (spawn a reviewer agent) before merging.
