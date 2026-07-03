# System Walkthrough — WhatsApp Lead Acquisition Backend

> **BuildWithPorus** — Automated WhatsApp lead generation for dental clinics.
> Version: **V2 (RC1)** · Live on Render: `https://whatsapp-acquisition-backend.onrender.com`
> Stack: Python 3.11 · FastAPI · SQLAlchemy 2.0 · Postgres (Supabase) · Airtable · Gemini (via 9Router)

This document is a code-level tour of how the backend works: what each file does, how a
message flows end-to-end, and how the data-store, multi-tenant, and AI-fallback layers fit
together. Read it top-to-bottom for onboarding, or jump to a section via the map below.

---

## Table of Contents

1. [What the system does](#1-what-the-system-does)
2. [High-level architecture](#2-high-level-architecture)
3. [Request lifecycle: an inbound WhatsApp message](#3-request-lifecycle-an-inbound-whatsapp-message)
4. [The data-store abstraction (the key design idea)](#4-the-data-store-abstraction-the-key-design-idea)
5. [Multi-tenancy](#5-multi-tenancy)
6. [The AI layer (9Router → Gemini fallback)](#6-the-ai-layer-9router--gemini-fallback)
7. [Background jobs](#7-background-jobs)
8. [Dashboard & analytics API](#8-dashboard--analytics-api)
9. [The scraper (lead source)](#9-the-scraper-lead-source)
10. [File-by-file reference](#10-file-by-file-reference)
11. [Configuration & deployment](#11-configuration--deployment)
12. [Data model](#12-data-model)
13. [Gotchas & sharp edges](#13-gotchas--sharp-edges)

---

## 1. What the system does

The backend runs an end-to-end **B2B customer-acquisition funnel over WhatsApp** for a
marketing agency selling lead-gen services to dentists (current niche: Dentists in Gurugram).

The five functional layers:

| Layer | Responsibility | Key file(s) |
|---|---|---|
| **Lead Source** | Scrape Google Maps for clinic phone numbers | `scraper.py` |
| **Conversation** | Send/receive WhatsApp messages via Meta Cloud API | `whatsapp_client.py`, `main.py` |
| **Qualification** | AI qualifies leads in Hinglish, scores them Hot/Warm/Cold | `gemini_client.py` |
| **Conversion** | Detect booking intent, sync Calendly bookings | `main.py`, `calendly_client.py` |
| **CRM / Storage** | Persist leads, messages, pipeline state | `db_client.py`, `airtable_client.py`, `models.py` |

The agency operator ("Lord") gets a WhatsApp alert on every hot lead, and a separate
React dashboard reads the analytics endpoints exposed here.

---

## 2. High-level architecture

```
                        ┌─────────────────────────────────────────────┐
   WhatsApp user ──────▶│  POST /webhook  (main.py)                    │
   (dentist)            │  1. verify HMAC signature                    │
                        │  2. dedup by message id                      │
                        │  3. resolve tenant by phone_number_id        │
                        │  4. persist inbound msg (SYNC, primary store)│
                        │  5. call AI → reply → send (SYNC)            │
                        │  6. score/extract/alert  (BACKGROUND TASK)   │
                        └───────────────┬─────────────────────────────┘
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          ▼                             ▼                             ▼
   ┌─────────────┐              ┌──────────────┐             ┌──────────────┐
   │ GeminiClient│              │  Store layer │             │ WhatsAppClient│
   │ 9Router →   │              │ (store.py)   │             │  Meta Graph  │
   │ Gemini SDK  │              │  ┌─────────┐ │             │  API v17.0   │
   └─────────────┘              │  │Postgres │ │             └──────────────┘
                                │  │Airtable │ │
                                │  │DualWrite│ │
                                │  └─────────┘ │
                                └──────────────┘

   Background scheduler (APScheduler):
     • follow_up_job     — hourly   — nudge leads stuck in "Contacted" >48h
     • calendly_sync_job — every 5m — mark Calendly bookings as "Booked"
```

FastAPI serves two distinct surfaces from the same app:
- **Webhook surface** (`/webhook`) — machine-to-machine, called by Meta.
- **Dashboard surface** (`/api/*`) — called by the React frontend, protected by an
  `X-API-Key` header resolved to a tenant.

---

## 3. Request lifecycle: an inbound WhatsApp message

This is the hot path. It lives in `receive_message()` at `main.py:735`.

1. **Signature verification** (`main.py:742`) — `X-Hub-Signature-256` is HMAC-verified
   against `WHATSAPP_APP_SECRET` (`verify_signature`, `main.py:625`). Missing/invalid → 403.

2. **Store construction** (`main.py:751`) — a `WebhookStore` wraps the primary store and
   (optionally) a secondary store plus the request's `BackgroundTasks`. Primary writes run
   synchronously; secondary writes are mirrored in the background so the webhook never
   blocks on the legacy CRM.

3. **Tenant routing** (`main.py:759`) — `phone_number_id` from the webhook metadata is
   resolved to a `ClientContext` via `tenant.resolve_context_by_phone_id`. If Postgres
   isn't configured or `MIGRATION_MODE=airtable`, it falls back to the single default
   client (`CLIENT_ID`, default `1`).

4. **Loop / spam guards** (`main.py:783`–`816`):
   - Messages from `LORD_PHONE_NUMBER` are dropped (prevents the operator's own alerts
     from creating a feedback loop).
   - Duplicate `message.id`s are skipped via the in-memory `_processed_message_ids` set
     (cleared when it exceeds 1000 entries — handles Meta's retry bursts).
   - Meta test numbers (`1555…`) and `BLOCKED_NUMBERS` are ignored.

5. **Lead resolution / auto-create** (`main.py:804`) — unknown senders get a lead row
   created automatically (`source="Inbound WhatsApp"`).

6. **Persist + status bump** (`main.py:832`) — the inbound message is appended; a
   `New Lead` transitions to `Contacted`.

7. **AI reply (synchronous)** (`main.py:845`–`849`):
   - `parse_conversation_history` turns the message log into role-tagged turns.
   - `generate_response_with_history` produces the Hinglish reply.
   - `whatsapp.send_message` sends it and returns the `wamid`, which is stored on the
     outbound message.

8. **Deferred analytics (background)** (`main.py:857`) — scoring, info extraction, status
   escalation to `Qualified`, and the hot-lead alert to the operator all run in
   `_process_analytics_and_extraction_bg` (`main.py:661`) so they stay off the critical
   path. Each step has its own try/except so one failure can't sink the others.

> **Why the split?** Sending the reply fast is what keeps the conversation feeling human.
> Scoring/extraction each cost an extra LLM round-trip, so they're pushed to
> `BackgroundTasks` — this is the "70% latency reduction" the release notes cite.

---

## 4. The data-store abstraction (the key design idea)

Every storage backend implements the **same interface**, so `main.py` and `scraper.py`
never know which one is live. That interface (informally):

```
get_lead, add_lead, get_all_leads, get_lead_by_id, _search,
update_lead_status, update_lead_status_by_id, update_lead_info,
update_lead_score, append_message, update_message_status,
get_contacted_leads, table.create()
```

Three concrete implementations:

| Class | File | Backend |
|---|---|---|
| `AirtableClient` | `airtable_client.py` | Airtable REST API (raw `requests`) |
| `DatabaseClient` | `db_client.py` | Postgres via SQLAlchemy ORM |
| `DualWriteStore` | `store.py` | Writes to both; reads from primary |

**Records always look like Airtable**, even from Postgres. `DatabaseClient._to_fields`
(`db_client.py:64`) maps ORM columns back to Airtable's exact field names — including the
literal key `"Phone number type"` (with spaces). This is what lets the backend swap stores
without touching a single field-access site.

### Store selection

`store.py` chooses the implementation at import time from `MIGRATION_MODE`:

- `airtable` (default) → `AirtableClient` only — zero risk, pre-migration.
- `dual` → `DualWriteStore` — writes fan out to both; **Postgres write failures are logged
  and swallowed** (`DualWriteStore._safe`, `store.py:109`) so a Supabase hiccup can never
  break the live pipeline.
- `postgres` → `DatabaseClient` only — post-cutover.

`get_store()` memoises a single module-level singleton for the app/scheduler/scraper.

### The webhook's special store

The webhook path does **not** use the singleton. It builds a fresh `WebhookStore`
(`webhook_store.py`) per request, because it needs the request-scoped `BackgroundTasks`
object to mirror secondary writes asynchronously. `get_primary_store()` /
`get_secondary_store()` (`store.py:122`) decide which two stores it wraps based on the
same `MIGRATION_MODE`.

> **Two mirroring mechanisms exist and they differ.** `DualWriteStore` (used by the
> scheduler/dashboard singleton) mirrors *inline* with errors swallowed. `WebhookStore`
> (used only by `/webhook`) mirrors via `BackgroundTasks`. Both keep the secondary store
> eventually-consistent, but through different paths — keep this in mind when debugging a
> missing shadow write.

---

## 5. Multi-tenancy

Implemented in `tenant.py` on top of the `clients` and `pipeline_stages` tables.

- **Deployment model**: one Render service per client, each with its own `CLIENT_ID` env
  var. There is no per-request tenant switching at the infra level — but the code *also*
  supports dynamic resolution (below) so a single service can host many tenants.
- **Per-request resolution** (F6 additions):
  - `resolve_context_by_phone_id` — webhook routing: maps `wa_phone_number_id` → tenant.
  - `resolve_context_by_api_key` — dashboard auth: SHA-256 of the raw key is matched
    against `clients.dashboard_api_key_hash`.
  - Both return a `ClientContext` bundling the `Client` row, a `GeminiClient` seeded with
    that client's `system_prompt`, and the client's won/lost pipeline stage names.
- **Graceful degradation**: if Postgres isn't configured, every `tenant.*` helper returns
  `None`/`[]` and callers fall back to hardcoded defaults (`["Booked"]`, `["Lost"]`, the
  default system prompt) — i.e. exactly the Phase 1–7 single-tenant behaviour.
- **Onboarding**: `POST /api/admin/clients` (`main.py:251`, guarded by `X-Admin-Secret`)
  creates a client, generates a dashboard API key (**returned once, only the hash is
  stored**), and seeds the five default pipeline stages.

---

## 6. The AI layer (9Router → Gemini fallback)

`GeminiClient` (`gemini_client.py`) is the conversation brain. Every AI call follows the
**same two-tier pattern**:

1. **Primary — 9Router** (`gemini_client.py:22`): an OpenAI-compatible LLM gateway
   (`NINEROUTER_BASE_URL`) with a 15s timeout. Used when `NINEROUTER_API_KEY` is set.
2. **Fallback — direct Gemini SDK** (`gemini-2.5-flash`): used if 9Router is unset or
   raises. If both fail, a friendly Hinglish "network issue" message is returned so the
   user is never left hanging.

Three AI operations:
- `generate_response_with_history` — the sales reply. Injects live IST date/time into the
  system prompt (`[Current Date]` / `[Current Time]` placeholders).
- `score_lead` — returns exactly `Hot` / `Warm` / `Cold` (cleaned & clamped).
- `extract_lead_info` — pulls `Name` + `Business_Name` as JSON.

The **system prompt** (`DEFAULT_SYSTEM_PROMPT`, `gemini_client.py:32`) defines the entire
Hinglish sales persona, objection-handling few-shots, and the Calendly booking link.
Per-tenant prompts override it via `tenant.get_gemini_for_client`.

The message log is a plain-text append-only blob with a strict line format that both the
parser and the ORM's `Lead.last_message` property reproduce exactly:

```
[YYYY-MM-DD HH:MM:SS] INBOUND (text): message body
[YYYY-MM-DD HH:MM:SS] OUTBOUND (text): reply body
[YYYY-MM-DD HH:MM:SS] SYSTEM (system): event note
```

> This format is a load-bearing contract. `airtable_client.append_message`,
> `models.Lead.last_message`, `gemini_client.parse_conversation_history`, and the
> dashboard's `_parse_messages` all depend on it byte-for-byte.

---

## 7. Background jobs

Two APScheduler jobs registered in `main.py` (`main.py:144`) and started in the FastAPI
`lifespan` handler:

| Job | Interval | What it does |
|---|---|---|
| `follow_up_job` | hourly | Finds leads stuck in `Contacted` >48h and sends a follow-up **template**. Runs per-tenant if Postgres is configured, else single-tenant. **Stays in DRY-RUN until `FOLLOWUP_TEMPLATE_NAME` (or the client's `followup_template`) is set** — because Meta requires an approved template for re-engagement. |
| `calendly_sync_job` | every 5 min | Pulls recent Calendly bookings (`calendly_client.py`), matches them to leads by phone, flips status to `Booked`, logs a `SYSTEM` message, and alerts the operator. |

The 48h eligibility check parses the timestamp off the **last line** of the message blob
(`_process_followups_for_client`, `main.py:74`).

---

## 8. Dashboard & analytics API

All `/api/*` read endpoints are protected by `require_api_key` (`main.py:173`) and
rate-limited via SlowAPI (120/min per client key).

| Endpoint | Purpose |
|---|---|
| `GET /api/stats/dashboard` | Totals (leads/booked/lost), conversion rate, 7-day activity |
| `GET /api/leads` | Lead list (optionally filtered by `stage`) |
| `GET /api/leads/{id}` | Lead detail + parsed conversation + score breakdown |
| `GET /api/leads/{id}/messages` | Normalised messages from the Postgres `messages` table |
| `PATCH /api/leads/{id}/stage` | Move a lead between pipeline stages |
| `GET /api/settings` · `PATCH /api/settings` | Per-tenant prompt / Calendly / phone-id config |
| `GET /api/analytics/funnel` | Lead counts by status (raw SQL, tenant-scoped) |
| `GET /api/analytics/response-time` | Avg/median/max reply gap via Postgres window functions |
| `GET /api/analytics/bookings` | Booking counts over the last 30 days |

Because raw Airtable records carry only a text blob, several dashboard fields are
**derived on the fly** by regex/heuristics in `main.py`: `_parse_city`, `_parse_interest`,
`_parse_messages`, `_derive_score_breakdown`, and the Hot/Warm/Cold → 90/50/10 score
mapping in `_format_lead_row` (`main.py:408`).

> The `/analytics/*` endpoints query Postgres **directly with raw SQL** and assume
> `SessionLocal` exists — they are Postgres-only and will error in pure `airtable` mode.

---

## 9. The scraper (lead source)

`scraper.py` is a standalone pipeline (run `python scraper.py`, cron-friendly):

```
fetch_from_google_maps (Apify) → clean_leads → dedup vs store → store_leads
```

- **Source registry** (`SOURCE_REGISTRY`, `scraper.py:186`) makes adding new sources a
  matter of writing one `fetch_from_<source>` function and registering it.
- **Phone cleaning** (`clean_phone`) normalises to a 10-digit Indian mobile (strips `91`/`0`
  prefixes, validates the leading digit is 6–9).
- **Dedup** checks the active store before inserting; address/rating is stashed as a seed
  `SYSTEM` message so it survives into the conversation log.
- Writes go through `get_store()`, so the scraper honours `MIGRATION_MODE` just like the
  webhook does.

---

## 10. File-by-file reference

### Core application
| File | Role |
|---|---|
| `main.py` | FastAPI app: webhook, dashboard/analytics endpoints, admin onboarding, schedulers, dashboard-formatting helpers |
| `config.py` | Loads every env var (`.env` via `python-dotenv`); central config surface |
| `whatsapp_client.py` | Meta Cloud API wrapper: send text/template, submit templates, daily-cap + human-delay guardrails |
| `gemini_client.py` | AI brain: 9Router→Gemini fallback, system prompt, reply/score/extract |
| `calendly_client.py` | Fetch recent Calendly bookings, extract invitee phone numbers |

### Storage
| File | Role |
|---|---|
| `store.py` | Store factory + `DualWriteStore`; selects backend from `MIGRATION_MODE` |
| `airtable_client.py` | Airtable REST implementation of the store interface |
| `db_client.py` | Postgres/SQLAlchemy implementation (Airtable-shaped records) |
| `webhook_store.py` | Request-scoped store that mirrors secondary writes via `BackgroundTasks` |
| `database.py` | SQLAlchemy engine/session factory (`init_engine`, `is_configured`, `get_db`) |
| `models.py` | ORM models: `Client`, `Lead`, `Message`, `PipelineStage` |
| `tenant.py` | Multi-tenant resolution + per-client Gemini/pipeline-stage helpers |
| `scraper.py` | Google-Maps lead scraper pipeline |

### Migrations & ops
| File | Role |
|---|---|
| `migrations/001_init.sql … 004_f6b_jobs.sql` | Raw SQL schema migrations (init → multi-tenant → onboarding → jobs) |
| `migrate_airtable_to_postgres.py` | One-off Airtable → Postgres backfill |
| `run_004_migration.py` | Runner for the 004 migration |
| `render.yaml` | Render service definition + env var manifest |
| `requirements.txt` | Pinned dependencies |

### Utilities, tests & scratch (mostly gitignored)
`full_prod_verification.py`, `prod_verification.py`, `verify_lead.py`,
`profile_webhook.py`, `stress_test.py`, `parse_logs.py`, `send_*_webhook.py`,
`query_wamid.py`, `hygiene.py`, and the `test_*.py` files are dev/verification scripts.
The `patch_*.py` files are historical one-off patch scripts. `.gitignore` excludes
`test_*.py`, `*.html`, `.env`, and the various agent-tooling dotfiles.

---

## 11. Configuration & deployment

- **Secrets**: everything is read from env vars in `config.py`. Locally via `.env`
  (gitignored); on Render via the dashboard (`sync: false` keys in `render.yaml`).
- **Hosting**: Render free tier, auto-deploy on push to `main`.
  Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
- **Database**: Supabase Postgres via `DATABASE_URL` (connection-pooler URI). The engine
  is tuned for the free tier (small pool, `pool_pre_ping`, 30-min recycle — `database.py:33`).

Key env vars (see `config.py` / `render.yaml` for the full list):

| Var | Meaning |
|---|---|
| `MIGRATION_MODE` | `airtable` \| `dual` \| `postgres` — selects the active store |
| `CLIENT_ID` | This service's tenant id (default `1`) |
| `DATABASE_URL` | Supabase Postgres URI (enables the whole Postgres layer) |
| `WHATSAPP_APP_SECRET` | Enables webhook signature verification |
| `NINEROUTER_API_KEY` / `_BASE_URL` / `_MODEL` | Primary LLM gateway |
| `GEMINI_API_KEY` | Fallback LLM |
| `ADMIN_SECRET` | Guards the client-onboarding endpoint (fails closed if unset) |
| `FOLLOWUP_TEMPLATE_NAME` | Approved Meta template; empty ⇒ follow-up job stays DRY-RUN |
| `LORD_PHONE_NUMBER` | Operator's number for hot-lead alerts + loop guard |
| `FRONTEND_URL` | Allowed CORS origin for the dashboard |

---

## 12. Data model

Defined in `models.py` (Postgres). Airtable mirrors the same logical shape in a single
`Leads` table with a `Last_Message` text blob.

- **`Client`** — a tenant/agency. Holds per-client config: `wa_phone_number_id`,
  `system_prompt`, `followup_template`, `calendly_link`, `dashboard_api_key_hash`,
  `is_active`, `admin_phone`, `calendly_api_token`.
  > ⚠️ `calendly_api_token` is currently plaintext — there's a `TODO` (`models.py:51`) to
  > encrypt it at rest before storing real per-client tokens.
- **`Lead`** — a prospect. `phone` is the unique key (stored without `+`/spaces). Carries
  `status`, `lead_score`, `business_name`, `client_id` FK. `last_message` is a **computed
  property** that reconstructs the exact Airtable text-blob from the `messages` rows.
- **`Message`** — append-only conversation log (replaces the Airtable blob). Direction is
  `INBOUND`/`OUTBOUND`/`SYSTEM`; tracks `wa_message_id` and delivery `status`.
- **`PipelineStage`** — per-client ordered stages with `is_won`/`is_lost` flags, so each
  client can customise their funnel without a code deploy. Defaults seeded on onboarding:
  `New Lead → Contacted → Qualified → Booked(won) → Lost(lost)`.

---

## 13. Gotchas & sharp edges

- **`"Phone number type"` is a real field name** (with spaces) — an Airtable artifact
  preserved everywhere, including Postgres-shaped records. Don't "fix" it.
- **The message-log line format is a contract** across 4+ modules (see §6). Changing it
  breaks history parsing, scoring context, and the dashboard.
- **`_processed_message_ids` is in-memory** — dedup state is per-process and lost on
  restart/redeploy. Fine for Meta's short retry window; not a durable idempotency store.
- **`WhatsAppClient` rate-limit counters are in-memory & per-instance** — the daily cap
  (50) and date rollover live on the object. Fresh clients are instantiated inside the
  background task (`main.py:682`), so the cap is not globally coordinated.
- **`follow_up_job` uses the module-level `store` singleton**, but the webhook uses a
  per-request `WebhookStore`. They can point at different effective backends depending on
  mode — verify which one you're actually hitting when debugging.
- **`/analytics/*` endpoints assume Postgres** (raw SQL against `SessionLocal`). They are
  not guarded for `airtable` mode and will error without `DATABASE_URL`.
- **Postgres write failures are intentionally swallowed** in `dual` mode
  (`DualWriteStore._safe`) — a shadow write can silently fail; check logs, not just data.
- **Calendly sync window is 2 hours** (`calendly_client.py:45`) and the job runs every
  5 min, so a booking is only picked up if it appears within that window.

---

*Generated from a code-level read of the backend as of V2 RC1 (2026-07-02). For the
build history and phase-by-phase plan, see `README.md`, `RELEASE_NOTES.md`, and
`v2_upgrade.md`.*