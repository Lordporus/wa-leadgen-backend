-- Phase 7 — initial Postgres schema for WhatsApp Leads Acquisition.
--
-- Run once against a fresh Supabase/Postgres database:
--     psql "$DATABASE_URL" -f migrations/001_init.sql
--
-- Safe to re-run (uses IF NOT EXISTS). Default tenant #1 is seeded so leads
-- created by Phase 7 (which carry client_id DEFAULT 1) satisfy the FK.

-- ── Default tenant (id = 1) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clients (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO clients (id, name)
VALUES (1, 'BuildWithPorus')
ON CONFLICT (id) DO NOTHING;

-- keep the serial counter ahead of any manually-inserted id
SELECT setval(pg_get_serial_sequence('clients', 'id'),
              GREATEST((SELECT MAX(id) FROM clients), 1));

-- ── Leads ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
    id            SERIAL PRIMARY KEY,
    phone         VARCHAR(20) UNIQUE NOT NULL,
    name          VARCHAR(255) DEFAULT 'WhatsApp User',
    source        VARCHAR(100),
    status        VARCHAR(50) DEFAULT 'New Lead',
    business_name VARCHAR(255),
    lead_score    VARCHAR(20),
    client_id     INTEGER REFERENCES clients(id) DEFAULT 1 NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_leads_status   ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_phone    ON leads(phone);
CREATE INDEX IF NOT EXISTS idx_leads_client_id ON leads(client_id);

-- ── Messages (normalised conversation log) ────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id         SERIAL PRIMARY KEY,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction  VARCHAR(10) NOT NULL,   -- INBOUND | OUTBOUND | SYSTEM
    msg_type   VARCHAR(20) DEFAULT 'text',
    body       TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_lead_id ON messages(lead_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

-- ── updated_at trigger (keeps leads.updated_at honest) ───────────────────
CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_leads_touch ON leads;
CREATE TRIGGER trg_leads_touch
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
