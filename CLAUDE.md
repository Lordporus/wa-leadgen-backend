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

## Sprint 6 — Billing, Metering & Beta Release (current)

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

### Task 4: Beta Release Readiness
- Self-serve signup flow (or confirm admin-only onboarding stays for beta — check docs)
- Basic usage dashboard endpoint: GET /api/billing/usage (current period usage vs limits)
- Verify all Sprint 1–5 security fixes are still in place (regression check, not new work)
- Read docs/13_PRODUCT_ROADMAP.md for beta scope — do not add features beyond documented M2 scope

## Sprint 7 (after Sprint 6 complete)
- Campaign/Drip Engine
- Advanced analytics
- Multi-channel support

## Technical Debt
- db_client.py background task methods (append_message, update_lead_info, update_message_status, update_lead_score) have client_id=None default for backward compat — must be updated in Sprint 3
- REDIS_URL defaults to localhost in config.py — must be set as env var on Render before deploying Redis queue to production

## Tech Stack
- Backend: FastAPI, Python, SQLAlchemy, Supabase (PostgreSQL)
- Frontend: Next.js, TypeScript, TailwindCSS
- AI: Gemini via 9Router
- Hosting: Render
- Queue: Redis + RQ (implemented Sprint 3)
