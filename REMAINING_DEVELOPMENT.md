# Remaining Development Plan & Analysis

Based on an analysis of the current project documentation (`niche.md`, `schema.md`, `migration.md`, `rate_limiting.md`, `whatsapp.md`, `v2_upgrade.md`, and `PRODUCTION_READY.md`), this file outlines the functions left for development, the technology stack required, and the orchestration setup needed for future phases.

---

## 1. Functions Left for Development (Implementation Plan)

### Core Backend & Database (Phases 8-11+)
*   **Postgres Final Cutover:** Complete the three-step migration from Airtable to Supabase Postgres (currently in shadow/dual mode). Decommission Airtable and remove `airtable_client.py`.
*   **Tenant-Aware Routing & Custom Stages:** Implement the `pipeline_stages` configuration table for customizable stage names per client (Phase 8).
*   **Supabase Vault Integration:** Encrypt sensitive credentials like `calendly_api_token` which are currently stored in plaintext (Sprint 11+).
*   **Strict Tenant Isolation:** Refactor `db_client.py` background methods to remove `client_id=None` defaults to ensure full data isolation across multi-tenant architecture.

### V2 Application Features (from `v2_upgrade.md`)
*   **Conversations Tab (F1):** Build the `/api/conversations` endpoint and the Next.js `ConversationsListView` page.
*   **Enhanced Lead Details (F2):** Implement persistence for AI-extracted `City`, `Interest`, and `Numeric_Score` (0-100), rather than re-deriving on every API call. Fix `_parse_messages()` to include SYSTEM messages.
*   **Pipeline Persistence (F3):** Wire the frontend Kanban drag-and-drop to call `PATCH /api/leads/{id}/stage` so state is saved.
*   **System Prompt Editor (F4):** Wire the AI settings page to `GET`/`PUT /api/settings` and enable hot-reloading of the `gemini` singleton.
*   **WhatsApp Message Status (F5):** Capture the `wamid` on send, persist `statuses` webhook events, and render delivery ticks (✓/✓✓) in the UI.
*   **Multi-Tenant Onboarding UI (F6):** Build `/api/clients` CRUD endpoints and a "Clients" management page for the agency owner.
*   **Analytics Dashboard (F7):** Create `/api/analytics` for response rates, conversation lengths, stage funnels, and best-hour heatmaps.

### Outbound & Integration
*   **WhatsApp Native Reminders (Phase 9+):** Add follow-up nudge logic. Meta template `dentist_followup_v1` is currently REJECTED and needs revision/approval.
*   **Billing Setup:** Execute `setup_plans()` to generate Razorpay base/agency plans, which is blocking checkout.

---

## 2. Technology Stack Needed (Paid/Free Status)

To implement the remaining development, you will need the following stack components:

| Technology | Purpose | Pricing Status |
| :--- | :--- | :--- |
| **Supabase (PostgreSQL)** | Final source-of-truth database & Vault for encryption | **Free Tier** available; scales to paid for high storage/compute. |
| **Redis (Upstash/Render)** | RQ Worker Queue (AI async replies) & slowapi rate-limiting | **Free Trial/Tier** available via Upstash; Render Redis is paid (~$7/mo). |
| **Render Web Services** | Hosting FastAPI backend & Next.js frontend | **Free Tier** available for Web; Background Worker requires paid. |
| **Sentry** | APM & Production Error Tracking | **Free Developer Tier** available. |
| **Razorpay** | Subscription & Billing | **Free to Setup**; charges per-transaction %. |
| **Meta Cloud API** | WhatsApp Business API messaging | **Paid** per conversation (first 1,000 service convs often free). |

---

## 3. Orchestration & Operations Required

For the next stages of development and scaling, the following orchestration mechanisms are required:

### A. Background Worker Orchestration
*   **RQ (Redis Queue):** The current webhook accepts messages but drops them because the Redis worker path is broken. You need a dedicated **Render Background Worker** running `rq worker` to asynchronously process AI responses (via `jobs.py`) without blocking the webhook acknowledgment.

### B. Horizontal Rate-Limiting
*   **Redis-backed slowapi:** Current rate limits reset on every server restart (in-memory). Upgrading `slowapi` to use a Redis URI is required to coordinate limits across multiple server instances once the app scales horizontally.

### C. Cron Jobs & Scheduling
*   **APScheduler / Render Cron:** Currently, Calendly polling runs in-process. Moving forward, nightly rollups (e.g., `analytics.py`), billing checks, and potential Phase 9+ WhatsApp reminders will require an external cron orchestrator (like Render Cron Jobs) to ensure high reliability.

### D. CI/CD Orchestration
*   Automated staging deployments and test suite execution (especially for tenant-isolation verification) need to be orchestrated via GitHub Actions prior to onboarding public clients.
