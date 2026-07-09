# AI Sales OS — Claude Code Context

## Role
You are the senior engineering partner for AI Sales OS. This is a production B2B SaaS platform.

## Source of Truth
All 16 documents in the /docs folder are the authoritative specification. Read them before implementing anything.

## Current State
- Audit complete: PROJECT_BASELINE_AUDIT.md in project root
- Sprint 1 in progress: Security fixes
- Tasks completed: Debug endpoints removed, HMAC mandatory, WhatsAppClient singleton bug fixed

## Rules
1. Never implement multiple unrelated things in one task
2. Always read the relevant doc before implementing
3. Always show what you changed and where
4. Never modify files outside the scope of the current task
5. After every task, state: what was done, what was changed, what is next

## Sprint 1 Remaining Tasks (do these in order)
- [x] Task 4: WhatsAppClient singleton fix (main.py)
- [x] Task 5: Remove hardcoded Calendly link from gemini_client.py DEFAULT_SYSTEM_PROMPT
- [x] Task 6: Basic AI output validation before whatsapp.send_message()
- [x] Task 7: Switch 9Router to HTTPS in config.py

## Sprint 2 — Authentication & Tenant Security (current)

### Task 1: JWT Authentication ✅
- [x] Add POST /auth/login endpoint to main.py
- [x] Accept: client api_key in request body
- [x] Validate against hashed key in clients table (already exists in tenant.py)
- [x] Return: signed JWT token with payload: client_id, tenant_id, role, exp (24hr)
- [x] Add verify_jwt() dependency function
- [x] Read docs/10_SECURITY_SPECIFICATION.md and docs/06_API_SPECIFICATION.md before implementing
- [x] Do NOT touch any existing endpoints yet

### Task 2: ORM Tenant-Scoping Middleware ✅
- [x] Add tenant_id filter to all SQLAlchemy queries in db_client.py
- [x] Every query touching leads/messages/pipeline_stages must include .filter_by(client_id=client_id)
- [x] Read docs/05_DATABASE_DESIGN.md before implementing

### Task 3: Basic RBAC ✅
- [x] Add role field to JWT payload (admin / agent)
- [x] Add require_admin() dependency
- [x] Protect /admin/* endpoints with require_admin()
- [x] Read docs/10_SECURITY_SPECIFICATION.md before implementing

## Sprint 3 — Infrastructure & Queue ✅

### Task 1: Alembic Setup ✅
- [x] Install alembic in requirements.txt
- [x] Run alembic init in backend/
- [x] Configure alembic.ini and env.py to use DATABASE_URL from config.py
- [x] Generate first migration from existing SQLAlchemy models (models.py)
- [x] Do NOT run migrations yet — generate only
- [x] Read docs/05_DATABASE_DESIGN.md before implementing

### Task 2: Redis Queue for Webhooks ✅
- [x] Add redis and rq to requirements.txt
- [x] Create backend/worker.py — RQ worker entry point
- [x] In main.py webhook handler: move all processing after HMAC verify + dedup check into an RQ job
- [x] Webhook endpoint must ACK Meta (return 200) before any LLM call
- [x] Read docs/09_BACKEND_SPECIFICATION.md and docs/11_INFRASTRUCTURE_SPECIFICATION.md before implementing
- **Note:** Worker deployment deferred — Render Background Worker costs $7/month minimum. Redis queue code is ready. Deploy worker when first paying customer onboards.

### Task 3: SKIPPED — Celery
RQ is sufficient for current scale. Celery migration deferred to Sprint 6 when load metrics demand it.

## Sprint 4 — AI Safety & Human Takeover ✅

### Task 1: AI Safety Guardrails (4-layer stack) ✅
- [x] Create backend/guardrails.py
- [x] Layer 1 — Input Scanner: block prompt injection attempts before sending to LLM
  - Detect patterns: "ignore previous instructions", "you are now", "jailbreak", "act as"
  - If detected: return safe refusal, do not call LLM
- [x] Layer 2 — Output Length/Content Check: already done in Sprint 1 Task 6 (empty + 4096 char check) — mark as complete
- [x] Layer 3 — Confidence Scoring: after LLM response, score it 0.0-1.0 based on:
  - Response length (too short = low confidence)
  - Contains placeholder text like [NAME], [DATE] = low confidence
  - Contains URLs not in system prompt = low confidence
  - If confidence < 0.6: route to human takeover instead of sending
- [x] Layer 4 — PII Guard: before sending to LLM, scan input for Aadhaar numbers, PAN numbers, credit card patterns — replace with [REDACTED]
- [x] Read docs/07_AI_ENGINE_SPECIFICATION.md before implementing
- [x] All 4 layers must be in guardrails.py — not in main.py or jobs.py

### Task 2: Human Takeover API ✅
- [x] Add is_human_takeover boolean field to leads table (via Alembic migration)
- [x] Add POST /api/leads/{lead_id}/takeover endpoint — sets is_human_takeover = True
- [x] Add POST /api/leads/{lead_id}/release endpoint — sets is_human_takeover = False
- [x] In jobs.py webhook handler — check is_human_takeover before calling LLM
  - If True: skip AI response entirely, log "human takeover active for lead {id}"
- [x] Read docs/06_API_SPECIFICATION.md before implementing

### Task 3: Wire Guardrails into Webhook Pipeline ✅
- [x] In jobs.py — call guardrails at correct points:
  - Input scanner BEFORE LLM call
  - PII guard BEFORE LLM call
  - Confidence scorer AFTER LLM response
  - If low confidence: trigger human takeover for that lead
- [x] Do not modify guardrails.py in this task — only jobs.py

## Sprint 5 — Knowledge Base & RAG ✅

### Task 1: Document Ingestion Pipeline ✅
- [x] Install pgvector extension in Supabase (SQL: CREATE EXTENSION IF NOT EXISTS vector)
- [x] Add documents table via Alembic migration:
  - id, client_id (FK), filename, chunk_index, content TEXT, embedding VECTOR(768), created_at
- [x] Create backend/ingestion.py:
  - chunk_text(text, chunk_size=500) — splits document into overlapping chunks
  - embed_text(text) — calls Gemini embedding API (gemini-embedding-001, output_dimensionality=768), returns 768-dim vector
  - ingest_document(client_id, filename, text) — chunks + embeds + stores in documents table
- [x] Read docs/07_AI_ENGINE_SPECIFICATION.md before implementing
- **Note:** Uses REST API directly for embeddings — google-generativeai==0.3.0 is too old for output_dimensionality. pypdf added to requirements.txt for Task 3.

### Task 2: RAG Query Pipeline ✅
- [x] Create backend/rag.py:
  - retrieve_context(client_id, query, top_k=3) — embeds query, does pgvector cosine similarity search, returns top 3 chunks above relevance threshold
- [x] In jobs.py — before LLM call, retrieve_context() and append to system prompt as context (restored after call to avoid mutating shared instance)
- [x] Read docs/07_AI_ENGINE_SPECIFICATION.md before implementing

### Task 3: Document Upload API ✅
- [x] Add POST /api/documents/upload endpoint in main.py
  - Accepts: multipart file upload (PDF or TXT)
  - Extracts text from PDF using pypdf
  - Calls ingest_document() from ingestion.py
  - Auth: require_api_key
- [x] Add GET /api/documents endpoint — list tenant's documents
- [x] Read docs/06_API_SPECIFICATION.md before implementing

## Sprint 6 — Billing, Metering & Beta Release ✅

### Task 1: Usage Tracking Foundation ✅
- [x] Add `usage_events` table via Alembic migration:
  - id, client_id (FK), event_type (message_sent, ai_response, document_ingested), 
    tokens_used, cost_estimate, created_at
- [x] Create backend/usage.py:
  - log_usage(client_id, event_type, tokens_used) — writes a usage_events row
  - get_monthly_usage(client_id) — aggregates current billing-period usage
- [x] Wire log_usage() calls into:
  - jobs.py (after LLM call — log tokens from Gemini response metadata)
  - ingestion.py (after embed_text() — log embedding tokens)
- [x] Read docs/13_PRODUCT_ROADMAP.md and docs/14_FINAL_PRD.md before implementing
- **Note:** Token counts are estimated (~4 chars/token) since gemini_client.py returns text only, not response metadata. Cost estimates use Gemini 2.5 Flash pricing. Migration 0004 not yet applied to production.

### Task 2: Hard Caps Enforcement ✅
- [x] Add `plan_limits` config (per client_id or per plan tier):
  - max_ai_responses_per_month, max_documents, max_tokens_per_month
- [x] Create check_limit(client_id, limit_type) in usage.py
- [x] Call check_limit() before:
  - Processing incoming WhatsApp message (jobs.py — step 4b2, after human takeover gate)
  - Document upload (main.py POST /api/documents/upload — before reading file)
- [x] On limit breach: 
  - Block the action, return clear error (or trigger human takeover for WhatsApp flow)
  - Log a "limit_exceeded" event
- [x] Read docs/10_SECURITY_SPECIFICATION.md for rate-limit/abuse patterns already in place
- **Note:** Plan limits are dict-based defaults (base: 1000 AI msgs, 50 docs, 500K tokens; agency: 5000/200/2M). Per-client plan_tier column added in Task 3 (Razorpay). WhatsApp cap triggers human takeover instead of error message.

### Task 3: Razorpay Integration ✅
- [x] Add razorpay SDK to requirements.txt
- [x] Create backend/billing.py:
  - create_subscription(client_id, plan) — creates a Razorpay Subscription via Subscriptions API
  - handle_webhook(payload, signature) — verifies Razorpay webhook signature 
    (HMAC SHA256 using webhook secret, per Razorpay's X-Razorpay-Signature header), 
    processes events: subscription.activated, subscription.charged, 
    subscription.pending, subscription.halted, subscription.cancelled, subscription.completed
  - setup_plans() — one-time utility to create Razorpay Plans via API (or use Dashboard)
- [x] Add POST /api/billing/webhook endpoint 
  - Verify signature BEFORE parsing payload (same pattern as WhatsApp HMAC check 
    in main.py — reject unverified requests immediately)
  - Rate limit like other public-facing endpoints (100/min)
- [x] Add POST /api/billing/checkout endpoint (creates Razorpay subscription, returns subscription_id + short_url for frontend)
- [x] Add columns to Client model: 
  - razorpay_customer_id, razorpay_subscription_id, subscription_status, plan_tier
- [x] Add migration 0005 for these new Client columns
- [x] Read docs/06_API_SPECIFICATION.md and docs/14_FINAL_PRD.md before implementing
- [x] Confirm plan_tier values match usage.py's PLAN_LIMITS keys ("base", "agency") exactly
- [x] Pricing confirmed with user: ₹4,999/mo (Base), ₹14,999/mo (Agency)
- [x] Hard cap checks in jobs.py and main.py now read client.plan_tier to use correct limits
- **Note:** Uses Razorpay Subscriptions API for true recurring billing (switched from Orders API). Plans created on Dashboard, IDs stored in RAZORPAY_PLAN_ID_BASE / RAZORPAY_PLAN_ID_AGENCY env vars. total_count=1200 for pseudo-indefinite monthly billing. subscription_status values: created, active, past_due (retrying), halted (retries exhausted), cancelled, completed. Migration 0005 not yet applied to production. Env vars needed: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET, RAZORPAY_PLAN_ID_BASE, RAZORPAY_PLAN_ID_AGENCY.

### Task 4: Beta Release Readiness ✅
- [x] Confirmed admin-only onboarding stays for beta (POST /api/admin/clients)
- [x] Onboarding endpoint now explicitly sets plan_tier="base", subscription_status="inactive"
- [x] Added GET /api/billing/status — returns plan_tier, subscription_status, usage summary + limits (auth: require_api_key)
- [x] Added GET /health — no auth, returns {"status", "db", "redis"} connectivity check
- [x] Read docs/13_PRODUCT_ROADMAP.md and docs/14_FINAL_PRD.md before implementing
- **Note:** Health check returns "degraded" if DB is unreachable (Redis being down is tolerable). Self-serve signup deferred to Q3 per roadmap — beta uses admin provisioning only.

## Sprint 7 — Analytics & Reporting (current)

### Task 1: Nightly Rollup Jobs ✅
- [x] Create backend/analytics.py
- [x] Function: rollup_daily_stats(client_id, date) — aggregates from leads + messages tables:
  - total_leads, new_leads, qualified_leads, booked_leads, lost_leads
  - total_messages, ai_messages, human_messages
  - avg_response_time_seconds
  - meetings_booked (leads where status = "Booked")
- [x] Store results in a new daily_stats table (via Alembic migration 0006)
- [x] daily_stats columns: id, client_id, date, stats JSONB
- [x] Schedule via APScheduler: run every night at 2 AM IST
- [x] Read docs/13_PRODUCT_ROADMAP.md before implementing
- **Note:** Migration 0006 NOT yet applied to production (generate-only, matching 0004/0005). `alembic heads` = 0006. Existing prod DB → apply with `alembic stamp`-awareness per prior runs.
- **Note:** No human-agent send path exists (takeover only *pauses* the AI), so ai_messages = OUTBOUND, human_messages = INBOUND (the prospect). SYSTEM messages count in total_messages only.
- **Note:** No status-history table — status buckets (new/qualified/booked/lost) are a point-in-time snapshot of leads *created* that IST day. qualified_leads includes Booked. booked_leads/lost_leads use tenant.get_won_stage_names / get_lost_stage_names (default ["Booked"]/["Lost"]).
- **Note:** created_at is UTC-naive; the rollup converts the IST calendar day to a UTC [start,end) window before querying. Job rolls up YESTERDAY (IST) and runs 02:00 Asia/Kolkata via CronTrigger (explicit tz — Render runs UTC). avg_response_time_seconds is None when no answerable outbound that day.

### Task 2: Dashboard KPI Endpoints ✅
- [x] Add these endpoints to main.py:
  - [x] GET /api/analytics/summary — returns last 30 days rollup for tenant
  - [x] GET /api/analytics/funnel — lead stage conversion counts
  - [x] GET /api/analytics/response-time — avg AI response time trend (7 days)
- [x] All endpoints: Auth require_api_key, tenant-scoped
- [x] Read docs/06_API_SPECIFICATION.md before implementing
- **Note:** `summary` (main.py:1147) and `response-time` (main.py:1227) read the pre-computed `daily_stats` table (from Task 1), not raw tables — matches analytics.py's design intent. `funnel` (main.py:1210) is a live current-snapshot of leads-by-status (pre-existing, spec-correct, left as-is).
- **Note:** `response-time` was previously a live window-function query over raw messages (14-day). Replaced with a 7-day rollup-based trend per spec. Days with no answerable outbound carry `avg_seconds: null` (not 0); window average is message-weighted. Old `/bookings` and `/sources` analytics endpoints untouched.
- **Note:** Both new endpoints key off IST calendar dates (matching daily_stats keys). Only main.py touched (+ one import: `timezone`).

### Task 3: Frontend Analytics Page ✅
- [x] Update frontend analytics page to consume new endpoints
- [x] Show: KPI cards (total leads, booked, conversion rate), funnel chart, response time chart
- [x] Use existing Recharts setup already in frontend
- [x] Read docs/08_FRONTEND_SPECIFICATION.md before implementing
- **Note:** Only `frontend/src/app/(dashboard)/analytics/page.tsx` touched. Added `/api/analytics/summary` SWR fetch → 3 KPI cards (Total Leads, Booked, Conversion Rate) from `summary.totals`, labeled "Last 30 days". Funnel (horizontal Recharts BarChart) already wired, left as-is. Response-time card FIXED: old code rendered `median_seconds`/`max_seconds` which Task 2 backend no longer returns — now shows single "Average (7 days)" stat + LineChart trend with `connectNulls={false}` so null days render as gaps (not false zeros). Auth is transparent: page calls relative `/api/...` via SWR `fetcher`; the Next.js proxy (`app/api/[...path]/route.ts`) injects `X-API-Key` server-side. Loading = shared skeletons (KPI row + charts); error = "Failed to load analytics data." Kept existing bookings + donut sections (backend endpoints untouched). No backend files modified. `tsc --noEmit` clean.

## Sprint 8 — Agency Sub-Accounts (current)

### Task 1: Agency Role & Sub-Account Model ✅
- [x] Add agency_id column to clients table (FK to clients.id, nullable) via Alembic migration 0007
- [x] Add role column to clients table: "owner" / "agency" / "sub_account" (default "owner")
- [x] Update models.py Client model with these two columns
- [x] Read docs/10_SECURITY_SPECIFICATION.md before implementing
- **Note:** Migration 0007 is GENERATE-ONLY, not applied to production (matching 0004/0005/0006). `alembic heads` = 0007, down_revision 0006. Existing prod DB → apply with `alembic stamp`-awareness per prior runs (see [[alembic-first-run]]).
- **Note:** `role` uses `server_default="owner"` + NOT NULL so all existing client rows backfill as standalone tenants with no data migration. `agency_id` is a self-referential FK (clients.id → clients.id), NULL for owners/agencies, set only on sub_account rows. Added `idx_clients_agency_id` index for the Task 3 rollup query (fetch all sub-accounts where agency_id = client.id). Security spec §1/§6.1/§7 confirm design: agencies see only their own sub-clients; rollups join child tenants only. `ForeignKey` already imported in models.py — no new imports.

### Task 2: Sub-Account Provisioning API ✅
- [x] Add POST /api/agency/sub-accounts endpoint (main.py:856):
  - Auth: require_api_key + client.role == "agency" (via require_agency dependency)
  - Creates a new Client row with agency_id = current client.id, role = "sub_account"
  - Returns new sub-account's id, name, api_key (plaintext, once) and dashboard_url
- [x] Add GET /api/agency/sub-accounts endpoint (main.py:944):
  - Returns list of all sub-accounts under current agency
- [x] Read docs/06_API_SPECIFICATION.md before implementing
- **Note:** Auth uses `require_api_key` (per Sprint 8 API-key model, matching Task 1/3), not JWT `require_admin`. New `require_agency` dependency wraps `require_api_key` and enforces `role == "agency"` → 403 otherwise. API-key generation reuses the exact onboarding pattern: `secrets.token_hex(32)` raw key + `hashlib.sha256(...).hexdigest()` stored in `dashboard_api_key_hash`; raw key returned once as `api_key`.
- **Note:** Sub-account creation seeds the same 5 default pipeline stages as onboarding so the child dashboard is usable immediately. `wa_phone_number_id` is optional here (agency may set it later) but still uniqueness-checked when supplied (409 on conflict). `dashboard_url` derives from `FRONTEND_URL` env var (null if unset — no phone-specific path since routing is API-key based). GET filters `agency_id == client.id AND role == "sub_account"`, ordered by id, returns `{sub_accounts: [...], count}`. Only main.py + this CLAUDE.md touched.

### Task 3: Cross-Tenant Rollup View ✅
- [x] Add GET /api/agency/analytics endpoint (main.py:974):
  - Auth: require_agency (require_api_key + role == "agency")
  - Aggregates daily_stats across all sub-accounts (agency_id = client.id)
  - Returns combined totals + per-sub-account breakdown
- [x] Read docs/06_API_SPECIFICATION.md before implementing
- **Note:** Reuses the last-30-days IST-keyed `daily_stats` aggregation from `analytics_summary` (main.py:1292), but over `client_id IN (agency's sub_account ids)` via a single `.in_()` query (backed by `idx_clients_agency_id`). Returns `{start_date, end_date, sub_account_count, totals, sub_accounts:[{id,name,totals}]}`. `totals` sums the 9 metric keys; `avg_response_time_seconds` is a message-weighted mean (None days skipped, never zero-filled — same honesty rule as summary); `conversion_rate` = booked/total ×100. Both combined and per-sub totals carry these derived fields. Agency's own row is never included (filter is `role == "sub_account"`); agencies with zero subs get empty totals + `sub_account_count: 0`. Only main.py + this CLAUDE.md touched — no new imports (Client/DailyStat imported locally, matching existing analytics endpoints).

## Sprint 9 (after Sprint 8 complete)
- Custom domains (Cloudflare integration)
- Theme customization per tenant
- Client-facing dashboards
- White-label branding

## Technical Debt
- db_client.py background task methods (append_message, update_lead_info, update_message_status, update_lead_score) have client_id=None default for backward compat — must be updated in Sprint 3
- REDIS_URL defaults to localhost in config.py — must be set as env var on Render before deploying Redis queue to production

## Tech Stack
- Backend: FastAPI, Python, SQLAlchemy, Supabase (PostgreSQL)
- Frontend: Next.js, TypeScript, TailwindCSS
- AI: Gemini via 9Router
- Hosting: Render
- Queue: Redis + RQ (implemented Sprint 3)
