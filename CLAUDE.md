# AI Sales OS — Claude Code Context

## Role
You are the senior engineering partner for AI Sales OS. This is a production B2B SaaS platform.

## Source of Truth
All 16 documents in the /docs folder are the authoritative specification. Read them before implementing anything.

## Current State
- All 10 sprints complete
- M3 Agency Beta ready
- Known gaps documented in PRODUCTION_READY.md

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

## Sprint 9 — White-Labeling & UI Polish (current)

### Task 1: Theme Customization API ✅
- [x] Add GET /api/settings/branding endpoint (main.py:378):
  - Auth: require_api_key
  - Returns: brand_color, logo_url, company_display_name from clients table
- [x] Add POST /api/settings/branding endpoint (main.py:395):
  - Auth: require_api_key
  - Accepts: brand_color (hex validation), logo_url, company_display_name
  - Updates clients table
- [x] Read docs/08_FRONTEND_SPECIFICATION.md before implementing
- **Note:** Dedicated branding endpoints carved out from the broader `/api/settings` PATCH (which still also writes these fields — left untouched for back-compat). GET returns the 3 white-label fields with the same defaults as `get_settings` (`brand_color` → "#C8A96E", `company_display_name` → name → "Leadgen CRM"). POST is a partial update: all 3 fields optional; only non-null fields written. Hex validated via module-level `_HEX_COLOR_RE = ^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$` (accepts #RGB and #RRGGBB, case-insensitive; value `.strip()`ed first) — invalid → 400 *before* any DB write. POST echoes back the persisted branding block. `re` + `BaseModel` already imported; no new deps. Spec §18 (tenant-overridable primary color via CSS vars) + §15 (white-label color/logo) confirm scope stays logo/color/name — no custom CSS (per Open Question §5). Only main.py + this CLAUDE.md touched.

### Task 2: Frontend White-Label Theme ✅
- [x] In frontend — read brand_color + company_display_name from /api/settings/branding on dashboard load
- [x] Apply brand_color as CSS variable --brand-color across dashboard
- [x] Replace hardcoded "AI Sales OS" title with company_display_name
- [x] Show logo_url in sidebar if set
- [x] Read docs/08_FRONTEND_SPECIFICATION.md before implementing
- **Note:** Only `frontend/src/app/(dashboard)/layout.tsx` touched. A branding scaffold already existed there but fetched the old `/api/settings`, set `--brand` (not `--brand-color`), and never rendered `logo_url`. Changes: (1) SWR fetch switched to `/api/settings/branding` (Task 1 endpoint, via the `[...path]` proxy which injects X-API-Key); (2) added `--brand-color` to the injected `:root` `<style>` block (kept `--brand`/`--brand-dim`/`--brand-bright`/`--gold` for back-compat with existing components); (3) sidebar now renders `<img src={logo_url} alt={company_display_name} class="max-h-8 w-auto object-contain">` when `logo_url` is set, else falls back to the text `<Logo>`. No literal "AI Sales OS" existed in the frontend — the hardcoded product name was "Leadgen CRM"; `company_display_name` already drives the sidebar title (text Logo label + footer) and now the logo `alt`. `tsc --noEmit` clean. No backend files touched.

### Task 3: UI Polish Pass ✅
- [x] Fix any hardcoded dental niche references in frontend (TD-5 from audit)
- [x] Replace hardcoded pipeline stages with dynamic fetch from /api/pipeline-stages
- [x] Ensure all dashboard pages handle empty states gracefully (no leads, no messages, no docs)
- [x] Read docs/08_FRONTEND_SPECIFICATION.md before implementing
- **Note:** Frontend-only task. Files: NEW `frontend/src/features/leads/hooks/useStages.ts`; edited `(dashboard)/leads/page.tsx`, `(dashboard)/settings/page.tsx`, `features/leads/components/KanbanBoard.tsx`, `features/leads/components/LeadProfileSidebar.tsx`. `tsc --noEmit` + `next lint` clean.
- **Note — dental (TD-5):** Audit's TD-5 root cause is *backend* (`_parse_interest` in main.py, hardcoded treatment regex) — left untouched since this task is frontend-scoped and backend edits were out of scope. Only ONE dental string existed in the frontend: settings branding placeholder `"e.g. Acme Dental CRM"` → changed to `"e.g. Acme Leads CRM"`. No other niche text (teeth/clinic/patient/etc.) anywhere in `frontend/src`.
- **Note — dynamic stages:** ⚠️ `GET /api/pipeline-stages` does NOT exist in the backend (verified: only stage source is `GET /api/settings` → `pipeline_stages:[{id,name,position,is_won,is_lost}]`). Built `useStages()` hook that reads from `/api/settings`, sorts by `position`, returns names; falls back to DEFAULT_STAGES `[New Lead, Contacted, Qualified, Booked, Lost]` before load / when tenant has none (Airtable/503 mode returns []). Wired into: leads filter chips, KanbanBoard columns (grid now `repeat(N,1fr)` not hardcoded 5), LeadProfileSidebar stage `<select>` (+ safety `<option>` so a lead's current stage stays selectable even if renamed out of config). Left color-only lookups (`utils.ts STAGE_COLORS`, analytics `STAGE_ORDER`, donut) as-is — they degrade to gold fallback for custom names and rewiring risks chart breakage; not interactive stage lists. If a dedicated `/api/pipeline-stages` route is added later, only `useStages.ts` changes.
- **Note — empty states:** Leads page → NEW `LeadsEmptyState` ("No leads yet" + WhatsApp hint; filter-aware variant "No leads in '{stage}'"). Conversations page → ALREADY had a polished "No conversations yet" empty state (unchanged). Documents ("no docs") → ⚠️ NO documents page exists in the frontend (nav is Dashboard/Leads/Conversations/Pipeline/Analytics/Settings; backend `GET /api/documents` exists but has no UI). Nothing to add an empty state to — a Documents page is net-new UI, out of scope for a polish task. Flagged for a future sprint.

## Sprint 10 — Production Hardening & M3 Release ✅

### Task 1: Sentry APM Integration ✅
- [x] Install sentry-sdk in requirements.txt
- [x] Initialize Sentry in main.py with SENTRY_DSN env var
- [x] Capture exceptions on all unhandled errors
- [x] Add SENTRY_DSN to config.py
- [x] Read docs/11_INFRASTRUCTURE_SPECIFICATION.md before implementing
- **Note:** `sentry-sdk[fastapi]>=2.0,<3.0` added to requirements.txt (FastAPI/Starlette ASGI integration auto-enabled by the extra — no manual middleware wiring needed; it captures all unhandled errors app-wide). `sentry_sdk.init()` runs in main.py (line ~44) BEFORE `app = FastAPI(...)` so the ASGI integration instruments every request; guarded by `if SENTRY_DSN:` so it's a clean no-op in local dev (logs "disabled"). config.py adds three vars: `SENTRY_DSN` (default "" = disabled), `SENTRY_ENVIRONMENT` (default "production"), `SENTRY_TRACES_SAMPLE_RATE` (default 0.0 → error capture on, perf tracing off until dialled up). `send_default_pii=False` set — avoids shipping lead PII to Sentry (matches security posture). Spec §13/§19-Phase2 confirm Sentry as the APM choice. Only main.py, config.py, requirements.txt + this CLAUDE.md touched. Env vars for Render: SENTRY_DSN (required to enable), optional SENTRY_ENVIRONMENT / SENTRY_TRACES_SAMPLE_RATE.

### Task 2: DB Indexing Review ✅
- [x] Read all SQLAlchemy models and identify missing indexes
- [x] Add indexes for high-frequency query patterns:
  - [x] leads.client_id + leads.status (composite) → `idx_leads_client_status`
  - [x] messages.lead_id + messages.direction (composite) → `idx_messages_lead_direction`
  - [x] daily_stats.client_id + daily_stats.date → CONFIRMED already exists (`idx_daily_stats_client_date`, from migration 0006) — not recreated
  - [x] usage_events.client_id + usage_events.created_at (composite) → `idx_usage_events_client_created`
- [x] Create Alembic migration 0008 for new indexes
- [x] Do NOT run migration yet
- **Note:** 3 NEW composite indexes added (daily_stats already had its composite). Declared in models.py (module-level `Index()` calls matching the existing `idx_messages_lead_id` style, so ORM metadata ↔ DB stay in sync) AND in migration `alembic/versions/0008_add_composite_indexes.py` (revision 0008, down_revision 0007). Verified via venv: all four tables register the expected index names; `alembic heads` = **0008 (head)**, linear chain `0007 -> 0008`. Migration is GENERATE-ONLY, NOT applied (matching 0004–0007); existing prod DB → apply with `alembic stamp`-awareness per [[alembic-first-run]]. Existing single-col `idx_messages_lead_id` left in place (the new composite's leading `lead_id` makes it redundant, but dropping it is out of scope). Migration uses plain transactional `CREATE INDEX` (tables tiny at beta scale); docstring flags `CREATE INDEX CONCURRENTLY` as the production-scale approach when row counts grow. Files touched: models.py, alembic/versions/0008_add_composite_indexes.py, this CLAUDE.md.

### Task 3: Missing Technical Debt Fixes ✅ (3 of 4; item 4 deferred by decision)
- [x] Add GET /api/pipeline-stages dedicated endpoint in main.py
- [x] Build Documents page in frontend (list uploaded docs, upload button)
- [x] Fix /api/settings PATCH hex validation on brand_color
- [~] Update db_client.py background task methods to pass client_id (remove None defaults) — **DEFERRED to its own task** (see Technical Debt). Not the one-liner it appears: the 4 methods are never called with client_id; every caller routes through `store.py` (DualWriteStore) + `webhook_store.py` wrappers whose signatures have NO client_id param. Making it required = threading client_id through 6–8 files (db_client, store, webhook_store, airtable_client + callers in jobs/main/profile_webhook/scraper), on the live webhook hot path + dual-write migration path. Some call sites lack client_id in scope (jobs.py appends inbound msg before deriving client_id; profile_webhook has none). Because `leads.phone` is globally `unique=True`, the None path resolves to exactly one lead today — latent risk, not a live tenant-leak. User decision: defer.
- **Note — item 1 (pipeline-stages):** `GET /api/pipeline-stages` added at main.py:391 (auth require_api_key, 120/min, tenant-scoped). Returns `{pipeline_stages:[{id,name,position,is_won,is_lost}]}` ordered by position (relationship already `order_by=position`). Frontend `useStages.ts` rewired from `/api/settings` → `/api/pipeline-stages` (response shape identical, so only URL + stale comment changed). Resolves the "useStages fetches from /api/settings" debt entry.
- **Note — item 2 (Documents page):** NEW `frontend/src/app/(dashboard)/documents/page.tsx` (route `/documents`). Lists docs from `GET /api/documents` (`[{filename,chunks,uploaded_at}]`); upload button → multipart `POST /api/documents/upload` via raw `fetch`+FormData (NOT apiFetch — that forces JSON content-type; browser must set the multipart boundary itself). States: loading skeletons, empty ("No documents uploaded yet"), inline upload error (parses backend `detail`), success → SWR `mutate()` refetch. Input `accept=".pdf,.txt"`, resets after pick so same file re-triggers. Nav entry + topbar title + IconFileText added to `(dashboard)/layout.tsx`. Resolves the "Documents page missing" debt entry. `tsc --noEmit` + `next lint` clean.
- **Note — item 3 (hex on PATCH):** `/api/settings` PATCH now validates `brand_color` against the same `_HEX_COLOR_RE` used by POST /api/settings/branding, BEFORE any DB write → 400 on invalid; value is `.strip()`ed before persist. Closes the gap where the legacy PATCH path could store an unvalidated color that the dedicated branding endpoint would reject.

### Task 4: Production Readiness Checklist ✅
- [x] Verify all items in PROJECT_BASELINE_AUDIT.md production blockers are resolved
- [x] Generate a PRODUCTION_READY.md report:
  - What was fixed (Sprint 1-10)
  - What is still pending
  - Deployment checklist for M3 Agency Beta release
- [x] Read PROJECT_BASELINE_AUDIT.md before implementing

## Post Sprint 10
- Multi-Channel: Voice AI, Cold Email (Sprint 11-16)
- Enterprise Scale: SSO, Read replicas (Sprint 17-24)

## Technical Debt
- db_client.py background task methods (append_message, update_lead_info, update_message_status, update_lead_score) have client_id=None default — **full scope (deferred from Sprint 10 Task 3):** making client_id required is a 6–8 file refactor. The methods are never called with client_id; callers route through store.py (DualWriteStore) + webhook_store.py wrappers that have NO client_id param, so threading it requires changing db_client.py, store.py, webhook_store.py, airtable_client.py + every call site (jobs.py ×4, main.py ×4, profile_webhook.py ×2, scraper.py ×1). Touches the live webhook hot path + dual-write path; some call sites lack client_id in scope (jobs.py appends inbound before deriving it; profile_webhook has none). Mitigated today by `leads.phone` being globally unique (None path → exactly one lead, no live cross-tenant leak). Do as a dedicated task with its own verification pass.
- REDIS_URL defaults to localhost in config.py — must be set as env var on Render before deploying Redis queue to production

## Tech Stack
- Backend: FastAPI, Python, SQLAlchemy, Supabase (PostgreSQL)
- Frontend: Next.js, TypeScript, TailwindCSS
- AI: Gemini via 9Router
- Hosting: Render
- Queue: Redis + RQ (implemented Sprint 3)
