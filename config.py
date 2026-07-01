import os
from dotenv import load_dotenv

# Load environment variables from .env file for local development
load_dotenv()

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")
WHATSAPP_SIMULATE_HUMAN_DELAY = os.getenv("WHATSAPP_SIMULATE_HUMAN_DELAY", "false").lower() == "true"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ── 9Router (OpenAI-compatible LLM gateway) ──────────────────────────────
NINEROUTER_API_KEY = os.getenv("NINEROUTER_API_KEY", "")
NINEROUTER_BASE_URL = os.getenv("NINEROUTER_BASE_URL", "http://13.234.20.175:20128/v1")
NINEROUTER_MODEL = os.getenv("NINEROUTER_MODEL", "wa-leadgen-backend-fallback-chain")

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

LORD_PHONE_NUMBER = os.getenv("LORD_PHONE_NUMBER")

# ── Phase 7: Postgres migration ───────────────────────────────────────────
# MIGRATION_MODE controls the active data store:
#   "airtable" (default) → Airtable only           (pre-migration, safe)
#   "dual"               → write both, read Airtable (shadow phase)
#   "postgres"           → Postgres only            (post-cutover)
MIGRATION_MODE = os.getenv("MIGRATION_MODE", "airtable")
DATABASE_URL = os.getenv("DATABASE_URL")

# ── Phase 8: Multi-tenant SaaS ────────────────────────────────────────────
# Per-service deployment model: each Render service has its own CLIENT_ID.
# Default = 1 (BuildWithPorus) — backward-compatible with all Phase 1-7 code.
CLIENT_ID = int(os.getenv("CLIENT_ID", "1"))

# Follow-up template — empty keeps follow-up job in DRY-RUN. Set once Meta
# approves the template (was hard-coded "pending approval" in Phase 6).
FOLLOWUP_TEMPLATE_NAME = os.getenv("FOLLOWUP_TEMPLATE_NAME", "")
DEFAULT_CLIENT_NAME = os.getenv("DEFAULT_CLIENT_NAME", "BuildWithPorus")

# ── Dashboard API auth ────────────────────────────────────────────────────
# Frontend sends X-API-Key header on every dashboard request.
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")

# ── F6: Admin onboarding endpoint auth ────────────────────────────────
# Separate secret for POST /api/admin/clients. Fail closed if unset.
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

# ── Inbound lead creation guard ───────────────────────────────────────────
# Numbers in this list will be silently dropped and never auto-created as
# leads. Add spam callers or known bot numbers here as plain strings
# (no + prefix needed, normalisation is done at call-site).
BLOCKED_NUMBERS: list[str] = []
