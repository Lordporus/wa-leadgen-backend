# Pre-Merge Blocker Fix Report

Date: 2026-07-27  
Branch: `refactor/folder-structure`  
Scope: blocking findings from the pre-merge review only

## Root causes and fixes

### 1. Lead ID consistency

Root cause: in dual mode, `/api/leads` replaced some Airtable record IDs with
Postgres integers when a phone match existed. This produced mixed response
types and broke routes such as stage update that still operated on Airtable
record IDs.

Fix:

- Dual/Airtable mode now consistently returns Airtable record IDs as strings.
- Postgres mode returns Postgres primary keys serialized as strings.
- The previous branch's numeric IDs remain accepted as a tenant-scoped legacy
  fallback in dual mode, so cached dashboard links do not fail immediately.
- Detail, stage, messages, takeover, release, email management, and manual-send
  routes resolve the public ID consistently.
- Stage update now receives the authenticated client and preserves tenant
  scoping.

### 2. Postgres lead detail

Root cause: the detail route converted a Postgres ID to a phone and then called
`store.get_lead(phone)` without the required `client_id`. It also raised a 404
inside a broad `except Exception`, which converted the 404 into a 500.

Fix:

- Postgres mode resolves directly through tenant-scoped
  `get_lead_by_id(..., client_id=...)`.
- Every Postgres lookup filters by `client_id`.
- `HTTPException` is never swallowed by the broad error handler.
- Missing and cross-tenant leads return 404.

### 3. Migration deployment safety and serialization

Root cause: running Alembic in every instance's start command gated startup but
did not serialize concurrent migration processes.

Fix:

- `scripts/run_migrations.py` holds one PostgreSQL transaction advisory lock
  while Alembic runs on the same connection.
- `render.yaml` runs that wrapper before Uvicorn.
- Concurrent instances wait on the same database-enforced lock.
- A failed or outdated migration prevents Uvicorn from starting.
- Existing migration files were not modified.
- The exact deployment requirements and non-destructive application rollback
  procedure are documented in `docs/RENDER_MIGRATION_DEPLOYMENT.md`.
- No migration was applied locally or remotely during this task.

### 4. Unsafe test collection

Root cause: legacy diagnostic files under `tests/` perform provider calls at
module import time, so ordinary pytest collection could contact or mutate live
systems.

Fix:

- Live diagnostics were moved to `debug/live_checks/` and renamed
  `check_*.py`, outside pytest's normal discovery tree.
- `pytest.ini` now discovers all legitimate tests under `tests/`.
- `tests/conftest.py` clears provider credentials before collection and blocks
  network connections in offline tests.
- Live/integration markers are registered.
- Useful diagnostics were retained; none were deleted.

### 5. Email campaign duplicate sends

This risk was introduced by this PR: the campaign engine and affected files do
not exist on `main`.

Fix:

- Migration `0012` adds a per-enrollment delivery run ID and durable
  `email_campaign_delivery_attempts` rows with `pending`, `sending`, `sent`,
  and `failed` states.
- Each scheduler claim commits the attempt and retry deadline before provider
  I/O. Crash recovery reuses the stored provider idempotency key.
- A committed `sent` attempt is not sent again.
- Re-enrollment creates a new run ID, so it cannot reuse an earlier execution's
  delivery identity.
- Due enrollment selection still uses `FOR UPDATE SKIP LOCKED`, one enrollment
  per transaction.
- `EMAIL_PLATFORM_ENABLED` remains `false`.

### 6. Airtable tenant isolation

Root cause: Airtable and dual-mode record-ID reads accepted `client_id` but
ignored it, so a known Airtable ID could be resolved through the wrong tenant
context.

Fix:

- Each Airtable client is bound to the configured deployment `CLIENT_ID`.
- Search, list, phone, record-ID, contacted-lead, and message reads reject a
  different tenant before any HTTP request.
- Dual-mode lookups and stage updates carry the authenticated client ID through
  the store boundary.
- Calls that omit `client_id` remain scoped to the configured tenant for
  backward compatibility with legitimate cached/internal lookups.

## Files changed

- `.gitignore`
- `app/api/routers/leads.py`
- `app/clients/airtable_client.py`
- `app/core/models.py`
- `app/email/email_campaigns.py`
- `app/email/email_client.py`
- `app/store/store.py`
- `alembic/env.py`
- `alembic/versions/0012_add_email_campaign_delivery_attempts.py`
- `scripts/run_migrations.py`
- `render.yaml`
- `pytest.ini`
- `tests/conftest.py`
- `tests/unit/__init__.py`
- `tests/unit/test_lead_id_contract.py`
- `tests/unit/test_email_campaign_idempotency.py`
- `tests/unit/test_airtable_tenant_isolation.py`
- `tests/unit/test_migration_serialization.py`
- `docs/RENDER_MIGRATION_DEPLOYMENT.md`
- `BLOCKER_FIX_REPORT.md`
- Live diagnostic scripts moved from `tests/` to `debug/live_checks/`.

## Tests added

- Dual-mode list → detail → stage update with one stable Airtable ID.
- Dual-mode messages, takeover, release, and manual send using that same ID.
- Postgres valid tenant lead.
- Postgres missing lead.
- Postgres cross-tenant denial.
- Postgres-mode string ID contract.
- Campaign row claim with `skip_locked`.
- Deterministic email idempotency header.
- Cross-tenant Airtable record-ID and cached phone lookup denial before network.
- Durable attempt recovery with the same key after a simulated crash.
- Crash-recovery send passes the persisted attempt key to the provider client.
- Completed-attempt suppression and unique re-enrollment run identities.
- Advisory transaction-lock ordering and rollback/release on migration failure.

## Verification executed

- Parsed 95 Python files under `app/`, `alembic/`, `scripts/`, `tests/`, and
  `debug/live_checks/` with `ast`: no syntax errors.
- `pytest --collect-only -q`: 15 offline unit tests only.
- `pytest -q`: 15 passed.
- Network-blocking fixture was active for every executed unit test.
- `alembic heads`: `0012 (head)`; no database connection or migration.
- Offline application import: 58 registered routes, scheduler not running.
- OpenAPI remains at the post-lead-contract baseline:
  `5ce521b4034cfa8054431e8d7b76d84eca56d56d4617ccda8dfbcfb7f95f874e`.
- These remaining fixes add no API contract changes: all 58 registered routes
  and all 54 OpenAPI method/path operations remain present.
- `git diff --check`: no whitespace errors.
- Migrations `0010` and `0011` are unchanged.
- Email remains disabled in `render.yaml`.
- No provider, Supabase, Render, or production migration command was contacted
  or executed.
- No secrets were added.

## Remaining risks

- The first Render deployment with the new gate must have a current database
  backup and the correct `DATABASE_URL`; the operator must confirm the Alembic
  step succeeds before accepting the deployment.
- Concurrent starters wait while holding one database connection each; verify
  the database connection budget before scaling beyond the current plan.
- In dual mode, routes that require Postgres-only data still depend on the
  Airtable lead having a matching tenant-scoped shadow row in Postgres.
- Campaign sending remains disabled and has not been live-tested against
  Resend. Provider idempotency retention is still a provider constraint, so it
  must stay disabled until the separate email activation review.
- The test environment reports an existing `datetime.utcnow()` deprecation
  warning in the campaign module; it is not a blocker for this scoped fix.

## Final verdict

**READY TO COMMIT**
