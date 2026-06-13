# Database Schema v1 — Airtable (MVP)

## Decision Context
Per Appendix B of the implementation plan, **Airtable is the accepted MVP
substitute for Postgres/Supabase** for Phases 1–6. It provides a UI, API,
formula filtering, and fast iteration without migrations. A formal SQL schema
(Postgres or Supabase) will be introduced during Phase 9 (Multi-Tenant SaaS).

---

## Base
- **Base ID:** `appOlYCuYnwTbE0Mr`
- **Base Name:** (Configured in Airtable account)

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

## Status Field — Allowed Values & Transitions

```
New Lead → Contacted → Qualified → Booked → Closed
                   ↘ Lost (at any stage)
```

| Status     | Set When |
|------------|----------|
| `New Lead` | Record created (scraper or inbound webhook) |
| `Contacted`| User sends any reply to our WhatsApp message |
| `Qualified`| AI response contains: "call", "connect", "team", "sure" |
| `Booked`   | AI response contains: "book", "appointment", "confirm", "time" |
| `Closed`   | Manually set by operator when deal is won |
| `Lost`     | Manually set by operator or future automation when lead goes cold |

---

## Future Tables (Phases 5–9, not yet implemented)

These tables are planned but do not exist yet:

| Table            | Purpose |
|------------------|---------|
| `clients`        | Agency clients (tenants) using the system |
| `pipeline_stages`| Config table for customisable stage names per client |
