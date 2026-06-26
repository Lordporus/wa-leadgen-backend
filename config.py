import os
from dotenv import load_dotenv

# Load environment variables from .env file for local development
load_dotenv()

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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
