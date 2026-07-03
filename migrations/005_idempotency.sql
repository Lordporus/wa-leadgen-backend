-- 005_idempotency.sql
-- Enforces a unique constraint on wa_message_id in the messages table
-- to ensure production-grade webhook deduplication.

ALTER TABLE messages
ADD CONSTRAINT unique_wa_message_id UNIQUE (wa_message_id);
