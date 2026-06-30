-- F6b Multi-tenant scheduler jobs

ALTER TABLE clients
  ADD COLUMN IF NOT EXISTS admin_phone VARCHAR(50),
  ADD COLUMN IF NOT EXISTS calendly_api_token VARCHAR(255);
