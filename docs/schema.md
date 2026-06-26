# Database Schema v1 — Airtable (MVP)

## Decision Context
Per Appendix B of the implementation plan, **Airtable is the accepted MVP
substitute for Postgres/Supabase** for Phases 1–6. It provides a UI, API,
formula filtering, and fast iteration without migrations.

**Phase 7** introduces Postgres (Supabase) as the relational backend, reached
via a three-step migration (`MIGRATION_MODE`: `airtable` → `dual` → `postgres`).
See [`migration.md`](./migration.md) for the full runbook. The Airtable `Leads`
table remains the source of truth until the `postgres` cutover; in `dual` mode
all writes are shadowed to Postgres.

The `Last_Message` long-text field is normalised into a `messages` table in
Postgres, but reconstructed in the identical text format on read so
`gemini_client.parse_conversation_history()` works unchanged.

---

## Base
- **Base ID:** `appOIYCuYnwTbE0Mr`
- **Base Name:** (Configured in Airtable account)

*Note: Environment variables required for Phase 6 include `CALENDLY_API_TOKEN` which must be stored in `.env` to allow the sync job to run.*

---

## Table: `Leads`

This is the single source of truth for all prospects in the system.

| Field Name          | Airtable Type      | Set By              | Purpose |
|---------------------|--------------------|---------------------|---------|
| `Name`              | Single line text   | Scraper / AI extract| Person or clinic name. Default `"WhatsApp User"` for inbound cold contacts. Updated by AI extraction if user mentions their name. |
| `Phone number type` | Phone number       | Scraper / Webhook   | Primary unique identifier for the lead. Used as the lookup key for all update operations. Stored without `+` prefix or spaces (e.g. `919876543210`). |
| `Source`            | Single line text   | Scraper / Webhook   | Origin of the lead. Values: `"Google Maps - Gurugram"` (scraper), `"WhatsApp Inbound"` (organic inbound). |
| `Status`            | Single select      | Webhook / Pipeline  | Current pipeline stage. Allowed values: `New Lead`, `Contacted`, `Qualified`, `Booked`, `Closed`, `Lost`. |
| `Business_Name`     | Single line text   | AI extraction       | Name of the clinic/business, extracted from conversation when user mentions it. |
| `Last_Message`      | Long text          | Webhook             | Append-only log of all conversation history. Stores timestamp, direction (INBOUND/OUTBOUND), type (text/template), and message body. Replaces the need for a separate Messages table in MVP. |
| `Lead_Score`        | Single line text   | AI extraction       | AI-generated score of the lead based on conversation (Cold / Warm / Hot). |
| `Created_At`        | Single line text   | Scraper / Webhook   | ISO 8601 timestamp of when the record was created. Set at creation, never updated. |

---

## Phase 7 — Postgres (Supabase) Schema

The Airtable single-table model is normalised into three relational tables.
Canonical schema lives in [`../migrations/001_init.sql`](../migrations/001_init.sql).

### Table: `clients` (tenants)
| Column      | Type         | Notes |
|-------------|--------------|-------|
| `id`        | SERIAL PK    | Default tenant `1 = BuildWithPorus` (seeded). |
| `name`      | VARCHAR(255) | Tenant / agency name. |
| `created_at`| TIMESTAMPTZ  | Default `NOW()`. |

### Table: `leads`
| Column         | Type         | Notes |
|----------------|--------------|-------|
| `id`           | SERIAL PK    | |
| `phone`        | VARCHAR(20)  | UNIQUE, NOT NULL. Lookup key (no `+`/spaces). |
| `name`         | VARCHAR(255) | Default `WhatsApp User`. |
| `source`       | VARCHAR(100) | e.g. `Google Maps - Gurugram`. |
| `status`       | VARCHAR(50)  | Default `New Lead`. Indexed. |
| `business_name`| VARCHAR(255) | AI-extracted. |
| `lead_score`   | VARCHAR(20)  | `Cold` / `Warm` / `Hot`. |
| `client_id`    | INT FK→clients | Default `1`. Phase 8 routing key. |
| `created_at`   | TIMESTAMPTZ  | Default `NOW()`. |
| `updated_at`   | TIMESTAMPTZ  | Auto-touched by trigger on UPDATE. |

### Table: `messages` (normalised `Last_Message`)
| Column      | Type         | Notes |
|-------------|--------------|-------|
| `id`        | SERIAL PK    | |
| `lead_id`   | INT FK→leads | `ON DELETE CASCADE`. Indexed. |
| `direction` | VARCHAR(10)  | `INBOUND` / `OUTBOUND` / `SYSTEM`. |
| `msg_type`  | VARCHAR(20)  | Default `text`; also `template`, `system`. |
| `body`      | TEXT         | Message content. |
| `created_at`| TIMESTAMPTZ  | Default `NOW()`. |

> **Compat note:** `Lead.last_message` (a Python property in `models.py`)
> reconstructs the exact Airtable text-blob format from these rows, so
> `gemini_client.parse_conversation_history()` and the follow-up timestamp
> parser in `main.py` work against Postgres data with zero changes.

---

## Status Field — Allowed Values & Transitions

```
New Lead → Contacted → Responded → Qualified → Booked → Closed
                   ↘ Lost (at any stage)
```

| Status      | Set When & Semantics |
|-------------|----------|
| `New Lead`  | Record created (scraped from Maps, never messaged) or organic inbound |
| `Contacted` | WE sent the initial outreach template. Waiting for reply. |
| `Responded` | Lead replied after being Contacted. Engaged in conversation. |
| `Qualified` | AI scored the conversation as Hot based on Need/Authority/Budget/Timeline |
| `Booked`    | Confirmed via Calendly API sync (Phase 6) — never auto-inferred from AI response |
| `Closed`    | Manually set by operator when deal is won / Placeholder for Phase 6 |
| `Lost`      | AI scored Cold + explicit disinterest signal, OR no response after follow-up |

---

## Future Tables (Phases 8+, not yet implemented)

These tables are planned but do not exist yet:

| Table            | Purpose |
|------------------|---------|
| `pipeline_stages`| Config table for customisable stage names per client (Phase 8) |

The `clients` table (tenants) is **already created in Phase 7** — every lead
carries a `client_id` FK defaulting to `1` (BuildWithPorus). Phase 8 makes
tenant-aware routing active.
