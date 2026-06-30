-- Phase 8 — F6: Multi-tenant client onboarding schema additions.
--
-- Run once after 002_multi_tenant.sql is already applied:
--     psql "$DATABASE_URL" -f migrations/003_onboarding.sql
--
-- Fully additive (IF NOT EXISTS / IF COLUMN NOT EXIST pattern). Safe to re-run.

-- ── Per-client dashboard API key (hashed) + admin metadata ───────────────
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS dashboard_api_key_hash  VARCHAR(64) UNIQUE,   -- SHA-256 hex of raw key
    ADD COLUMN IF NOT EXISTS admin_note              TEXT,                  -- internal: who/when onboarded
    ADD COLUMN IF NOT EXISTS is_active               BOOLEAN DEFAULT TRUE;  -- soft-disable a tenant

-- ── Enforce uniqueness on wa_phone_number_id (webhook routing key) ───────
-- Partial unique index: only non-NULL values must be unique (existing NULL
-- rows from Phase 7/8 are harmless).
CREATE UNIQUE INDEX IF NOT EXISTS uidx_clients_wa_phone
    ON clients(wa_phone_number_id)
    WHERE wa_phone_number_id IS NOT NULL;
