-- Phase 8 — Multi-Tenant SaaS: per-client config + pipeline stages.
--
-- Run once after 001_init.sql is already applied:
--     psql "$DATABASE_URL" -f migrations/002_multi_tenant.sql
--
-- Fully additive (IF NOT EXISTS / IF COLUMN NOT EXIST pattern). Safe to re-run.

-- ── Extend clients with per-client config ────────────────────────────────────
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS wa_phone_number_id VARCHAR(50),   -- Meta phone number ID
    ADD COLUMN IF NOT EXISTS system_prompt      TEXT,          -- Gemini sales persona
    ADD COLUMN IF NOT EXISTS followup_template  VARCHAR(100),  -- WhatsApp template name
    ADD COLUMN IF NOT EXISTS calendly_link      VARCHAR(255);  -- booking URL

-- Seed client #1 with existing hardcoded values so nothing breaks on first boot
UPDATE clients SET
    calendly_link     = 'https://calendly.com/buildporus/30min',
    followup_template = ''          -- empty = DRY-RUN (matches Phase 6/7 behaviour)
WHERE id = 1 AND calendly_link IS NULL;

-- ── Pipeline stages (per-client, ordered) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_stages (
    id        SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name      VARCHAR(100) NOT NULL,
    position  SMALLINT NOT NULL,
    is_won    BOOLEAN DEFAULT FALSE,   -- e.g. "Booked"
    is_lost   BOOLEAN DEFAULT FALSE,   -- e.g. "Lost"
    UNIQUE (client_id, name)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_client ON pipeline_stages(client_id, position);

-- Seed default stages for tenant #1 (mirrors the hardcoded statuses in main.py)
INSERT INTO pipeline_stages (client_id, name, position, is_won, is_lost) VALUES
    (1, 'New Lead',  1, FALSE, FALSE),
    (1, 'Contacted', 2, FALSE, FALSE),
    (1, 'Qualified', 3, FALSE, FALSE),
    (1, 'Booked',    4, TRUE,  FALSE),
    (1, 'Lost',      5, FALSE, TRUE)
ON CONFLICT (client_id, name) DO NOTHING;
