-- create_app_role.sql — provision/refresh the RLS-bound application role.
--
-- PURPOSE
--   The bootstrap superuser (`dentiva`) has rolbypassrls=t, so Row-Level
--   Security is NOT enforced when the app connects as it. For real tenant
--   isolation in production the app must connect as `dentiva_app`
--   (NOSUPERUSER, NOBYPASSRLS). Alembic migration b2c3d4e5f6a7 already creates
--   this role with a DEFAULT password ('dentiva_app'); this script is how the
--   operator sets a STRONG password and (re)applies grants on prod.
--
-- SAFETY
--   * Idempotent — safe to run more than once.
--   * Additive — creates/grants only; drops nothing.
--   * Does NOT change the app's connection string. Switching prod over to this
--     role is a separate, reversible step — see _docs/RLS_CUTOVER.md.
--
-- HOW TO RUN (operator, once, against the PROD database)
--   1. Replace CHANGE_ME below with a strong generated password.
--   2. psql "$DATABASE_URL_SUPERUSER" -f scripts/create_app_role.sql
--      (or paste into Railway's Postgres query console).
--   3. Build the dentiva_app connection string with that password and follow
--      the cutover runbook.
--
-- NOTE: keep the chosen password OUT of git. Put it only in the Railway env
--   (the new DATABASE_URL) and your private secrets store.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dentiva_app') THEN
        CREATE ROLE dentiva_app LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

-- Set a strong password (REPLACE 'CHANGE_ME') and re-assert the safety attrs in
-- case the role pre-existed with weaker settings.
ALTER ROLE dentiva_app WITH LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'CHANGE_ME';

-- Schema + table privileges (no DDL: the app never creates/drops tables;
-- migrations keep running as the superuser).
GRANT USAGE ON SCHEMA public TO dentiva_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dentiva_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dentiva_app;

-- Future tables/sequences created by the migration role inherit the same grants
-- so a new migration doesn't lock the app out of a new table.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dentiva_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO dentiva_app;

-- Verify (optional): should print rolbypassrls = f and rolsuper = f.
--   SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname='dentiva_app';
