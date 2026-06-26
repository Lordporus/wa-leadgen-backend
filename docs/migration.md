# Phase 7 — Airtable → Postgres (Supabase) Migration Runbook

This backend ships with a **dual-write migration path**. You move from Airtable
to Postgres with zero downtime and a fully reversible cutover.

The active store is chosen at import time by the `MIGRATION_MODE` env var:

| `MIGRATION_MODE` | Reads from   | Writes to                  | When to use |
|------------------|--------------|----------------------------|-------------|
| `airtable` (default) | Airtable  | Airtable                   | Pre-migration; zero behaviour change. |
| `dual`           | Airtable      | Airtable **+ Postgres**    | Shadow phase; verify Postgres parity. |
| `postgres`        | Postgres     | Postgres                   | Post-cutover; Airtable retired. |

`store.get_store()` returns the right object; `main.py` and `scraper.py` never
need to know which backend is active.

---

## Architecture

```
              ┌─────────────┐
   main.py ──▶│  store.py   │── get_store() picks one of:
   scraper.py │ get_store() │   • AirtableClient          (airtable)
              └─────┬───────┘   • DualWriteStore          (dual)
                    │           • DatabaseClient          (postgres)
        ┌───────────┼──────────────┐
        ▼                          ▼
  AirtableClient            DatabaseClient ──▶ Supabase Postgres
  (REST, source of          (SQLAlchemy 2.0, models.py)
   truth in dual mode)
```

Both clients expose the **identical interface**, returning records in the
Airtable shape `{"id", "fields": {...}}` so field-access code is unchanged.

---

## Runbook

### 0. Prerequisites
- Phase 7 code deployed.
- `MIGRATION_MODE` unset or `airtable` (verify no behaviour change first).

### 1. Provision Supabase
1. Create a project at supabase.com.
2. Under **Project Settings → Database → Connection string**, copy the
   **Session pooler / Transaction pooler** URI (looks like
   `postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:6543/postgres`).
3. Set it as `DATABASE_URL` in `.env` (locally) and in Render env vars.

### 2. Apply the schema
```bash
psql "$DATABASE_URL" -f migrations/001_init.sql
```
This creates `clients`, `leads`, `messages`, indexes, and the `updated_at`
trigger, and seeds the default tenant (`id=1, BuildWithPorus`). Idempotent.

### 3. Backfill existing Airtable data
```bash
python migrate_airtable_to_postgres.py
```
Pulls every Airtable lead, parses each `Last_Message` blob into individual
`messages` rows, and writes them to Postgres. **Idempotent** — re-running skips
phones already present. Prints a reconciliation report at the end:

```
  Airtable leads fetched : 142
  Postgres leads inserted: 142 (skipped 0 dupes)
  Messages parsed        : 871
  Postgres totals now    : 142 leads, 871 messages
  Reconciliation OK: fetched == inserted + skipped.
```

If the reconciliation line says **MISMATCH**, investigate before proceeding.

### 4. Enable dual-write (shadow phase)
Set `MIGRATION_MODE=dual` in Render (and `.env` locally) and redeploy.

- All writes now hit **both** Airtable and Postgres.
- Reads still come from Airtable (source of truth).
- Postgres write errors are **logged but never raised** — a Supabase hiccup
  cannot break the live WhatsApp pipeline.
- Watch logs for `[DualWrite] Postgres … failed` for ~24–48h.

### 5. Cut over to Postgres
Once you're confident Postgres is in sync:
1. Set `MIGRATION_MODE=postgres` and redeploy.
2. Reads + writes now go to Postgres only.
3. Spot-check a few leads via the webhook flow.

### 6. (Optional) Decommission Airtable
After a stable period on Postgres, remove Airtable credentials and the
`airtable_client.py` dependency. Leave the file in place until you're sure.

---

## Rollback
At any point before step 5, set `MIGRATION_MODE=airtable` and redeploy.
Airtable resumes as the sole store; no data is lost because Airtable was the
dual-write primary throughout.

---

## What got fixed alongside the migration
- **`gemini.extract_lead_info()` is now wired** — inbound messages trigger name
  / business extraction, populating `Name` and `Business_Name`.
- **Follow-up job is no longer permanently dry-run** — it sends the template
  named in `FOLLOWUP_TEMPLATE_NAME` if set, else logs `[DRY-RUN]`. Set the env
  var once Meta approves a template.
- **`scraper.py`** no longer uses the `.table.create()` field-dump hack; it
  calls `add_lead()` + `append_message()` like the rest of the codebase.
