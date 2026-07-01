---
name: add-migration
description: Create and validate an Alembic migration for the Dentovox backend. Use whenever a model changes — new table, column, index, or a data backfill — so the schema change is reversible, tested, and RLS-covered.
---

# Add a migration

1. **Change the model** first (`app/models/*.py`). For a new PHI column use
   `EncryptedString`; declare indexes in `__table_args__` (single source for
   create_all + the migration).
2. **Write the revision** in `migrations/versions/<rev>_<slug>.py`:
   - Set `down_revision` to the current head (`alembic heads`).
   - Implement BOTH `upgrade()` and `downgrade()`.
   - Column adds: `op.add_column(...)`; indexes: `op.create_index(...)` matching the
     model's `__table_args__` name.
   - **Backfill in the same migration** when new code reads the column immediately
     (avoid a NULL window). App crypto is importable in migrations, e.g.
     `from app.utils.crypto import decrypt_pii, phone_hmac` — runs as the superuser
     (bypasses RLS), so it sees every practice's rows. See
     `migrations/versions/x9s0t1u2v3w4_phone_hmac_index.py`.
3. **New table → RLS.** Add it to the RLS-enforced list in BOTH `tests/conftest.py`
   and `scripts/check_rls_coverage.py`, and FORCE RLS in the migration, or the
   tenant-isolation gate fails.
4. **Validate on real Postgres** (never trust autogenerate blindly):
   ```bash
   export DATABASE_URL="postgresql+asyncpg://dentiva:dentiva@localhost:5432/dentiva"
   alembic upgrade head && alembic downgrade -1 && alembic upgrade head
   ```
   Confirm one head: `alembic heads` shows exactly one.
5. `ruff check migrations/` clean; run the affected tests.

Never edit an already-applied migration's schema in place — add a new revision.
