-- 006: White-label branding fields on clients table
ALTER TABLE clients ADD COLUMN IF NOT EXISTS brand_color VARCHAR(20) DEFAULT '#10B981';
ALTER TABLE clients ADD COLUMN IF NOT EXISTS logo_url VARCHAR(500);
ALTER TABLE clients ADD COLUMN IF NOT EXISTS company_display_name VARCHAR(255);
