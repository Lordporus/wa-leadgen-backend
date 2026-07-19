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
NINEROUTER_BASE_URL = os.getenv("NINEROUTER_BASE_URL", "https://13.234.20.175:20128/v1")
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

# ── JWT Authentication ───────────────────────────────────────────────────
# Shared signing secret between backend and Next.js frontend (jose).
# Must match the frontend JWT_SECRET env var. Fail closed if unset.
JWT_SECRET = os.getenv("JWT_SECRET", "")


# ── F6: Admin onboarding endpoint auth ────────────────────────────────
# Separate secret for POST /api/admin/clients. Fail closed if unset.
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

# ── Redis Queue ──────────────────────────────────────────────────────
# Used by RQ workers to process webhook jobs off the HTTP hot path.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Inbound lead creation guard ───────────────────────────────────────────
# Numbers in this list will be silently dropped and never auto-created as
# leads. Add spam callers or known bot numbers here as plain strings
# (no + prefix needed, normalisation is done at call-site).
BLOCKED_NUMBERS: list[str] = []

# ── Razorpay Billing ─────────────────────────────────────────────────────
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_PLAN_ID_BASE = os.getenv("RAZORPAY_PLAN_ID_BASE", "")
RAZORPAY_PLAN_ID_AGENCY = os.getenv("RAZORPAY_PLAN_ID_AGENCY", "")

# ── Sentry APM (error + performance monitoring) ──────────────────────────
# SENTRY_DSN empty → Sentry disabled (safe local-dev / no-op default).
# Set the DSN in Render to enable. traces_sample_rate defaults to 0.0 so
# enabling the DSN turns on error capture without incurring tracing cost
# until performance sampling is explicitly dialled up.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production")
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))

# ── Email Outreach (E0 config · E1 schema · E2 send) ─────────────────────
# Locked product decisions for the email channel:
#   1. Provider: Resend (HTTP via existing `requests` dep — no Resend SDK).
#   2. Credentials: platform-managed keys only; per-tenant BYOK deferred.
#   3. Lead model: phone remains required; email is optional (schema in E1).
#   4. Cold blasts without opt-in: not supported.
#   5. AI auto-reply + sequences: later phases (E6 / E7).
# EMAIL_PLATFORM_ENABLED=false keeps prod no-op until Resend domain is ready.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend").strip().lower()
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
# Public backend base URL (no trailing slash) for unsubscribe links + webhook docs.
# Example: https://whatsapp-acquisition-backend.onrender.com
PUBLIC_API_URL = (os.getenv("PUBLIC_API_URL", "") or "").rstrip("/")
# Optional secret for signed unsubscribe tokens. If empty, E3 falls back to JWT_SECRET.
EMAIL_UNSUB_SECRET = os.getenv("EMAIL_UNSUB_SECRET", "")
# Platform kill switch — must be true AND API key present for email to be "ready".
# Per-tenant enable flags land on clients table in E1.
EMAIL_PLATFORM_ENABLED = os.getenv("EMAIL_PLATFORM_ENABLED", "false").lower() == "true"
# Conservative daily outbound cap (in-process counter pattern, like WhatsApp).
EMAIL_DAILY_CAP = int(os.getenv("EMAIL_DAILY_CAP", "50"))
# Optional platform default From identity (tenant overrides come in E1 schema).
EMAIL_DEFAULT_FROM_ADDRESS = os.getenv("EMAIL_DEFAULT_FROM_ADDRESS", "").strip()
EMAIL_DEFAULT_FROM_NAME = os.getenv("EMAIL_DEFAULT_FROM_NAME", "").strip()
# Resend REST API base (override only for tests/proxies).
RESEND_API_BASE_URL = (
    os.getenv("RESEND_API_BASE_URL", "https://api.resend.com") or "https://api.resend.com"
).rstrip("/")


def email_is_configured() -> bool:
    """True when platform email is enabled and the active provider has credentials.

    Does not imply schema/API (E1/E2) are deployed — only that env is ready.
    """
    if not EMAIL_PLATFORM_ENABLED:
        return False
    if EMAIL_PROVIDER == "resend":
        return bool(RESEND_API_KEY)
    return False


# Phase E5: AI email drafts. Auto-send after draft is OFF by default (human reviews).
# When true, POST /api/email/draft with send=true may send if confidence passes.
EMAIL_AI_AUTO_SEND = os.getenv("EMAIL_AI_AUTO_SEND", "false").lower() == "true"

# Phase E6: AI auto-reply on inbound email (after store + guardrails + confidence).
# Default true to mirror WhatsApp reply behaviour; set false to store-only.
EMAIL_AI_AUTO_REPLY = os.getenv("EMAIL_AI_AUTO_REPLY", "true").lower() == "true"
