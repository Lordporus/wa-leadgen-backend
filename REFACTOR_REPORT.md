# Phase 1.1 Refactor Report — Safe Monolith Extraction

**Date:** 2026-07-26
**Scope:** Backend route/module structure only

## Outcome

`main.py` is now the FastAPI composition root. It keeps application creation,
middleware, CORS, lifespan, and scheduler registration, then registers
feature-specific `APIRouter` modules.

No route handler body, business rule, database model, AI implementation,
email implementation, or WhatsApp provider implementation was changed.

## Files moved

The following handler groups were moved out of `main.py`:

| Original location | New location | Responsibility |
|---|---|---|
| `main.py` | `app/api/routers/auth.py` | Login |
| `main.py` | `app/api/routers/settings.py` | Settings, branding, pipeline stages, API-key rotation, templates |
| `main.py` | `app/api/routers/email.py` | Email settings, send/draft, Resend webhook, unsubscribe |
| `main.py` | `app/api/routers/campaigns.py` | Email campaigns, steps, enrollments, analytics |
| `main.py` | `app/api/routers/admin.py` | Admin client onboarding |
| `main.py` | `app/api/routers/leads.py` | Dashboard stats, leads, messages, stages, takeover, manual send, lead email |
| `main.py` | `app/api/routers/agency.py` | Agency sub-accounts and aggregate analytics |
| `main.py` | `app/api/routers/documents.py` | Knowledge-base document upload/list |
| `main.py` | `app/api/routers/billing.py` | Checkout, webhook, billing status |
| `main.py` | `app/api/routers/health.py` | Root and infrastructure health |
| `main.py` | `app/api/routers/whatsapp.py` | Meta verification and WhatsApp webhook |
| `main.py` | `app/api/routers/analytics.py` | Analytics summary, funnel, response time, bookings, sources |
| `main.py` | `app/api/dependencies.py` | Shared auth and rate-limit dependencies |
| `main.py` | `app/api/runtime.py` | Existing provider/store/runtime singletons |

## Routers created

- `auth.router`
- `settings.router`
- `settings.account_router` — separate registration preserves the original
  route order around email/campaign endpoints.
- `email.router`
- `campaigns.router`
- `admin.router`
- `leads.router`
- `agency.router`
- `documents.router`
- `billing.router`
- `health.router`
- `whatsapp.router`
- `analytics.router`

## Files untouched

- `app/core/models.py` and all database models
- `app/core/database.py`
- Every Alembic and legacy SQL migration
- `app/clients/gemini_client.py` and all AI logic
- `app/services/jobs.py`, `guardrails.py`, `rag.py`, and `ingestion.py`
- All files under `app/email/`
- `app/clients/whatsapp_client.py`
- Store implementations under `app/store/`
- Billing, analytics, usage, tenant, and other business services
- `worker.py`, deployment configuration, environment configuration, tests,
  scripts, debug utilities, and the complete frontend

## Compatibility verification

- Application import/startup: passed with the existing project environment.
- Live Uvicorn startup and shutdown: passed.
- Registered routes: **58 before / 58 after**, in identical order.
- Route handlers checked: **54**, with no missing or changed function bodies.
- All original top-level functions checked: **77**, with no missing or changed
  function bodies.
- OpenAPI contract: exact match.
  - Before SHA-256:
    `803ca9d7445b6b12dede715aeb33341eb14bd4b0ca57122e718f85d4137923d8`
  - After SHA-256:
    `803ca9d7445b6b12dede715aeb33341eb14bd4b0ca57122e718f85d4137923d8`
- Syntax compilation: passed for `main.py` and all `app/api/` modules.
- Static unresolved-global check: passed.
- Static unused-import check: passed.
- Static import-cycle check: passed.
- Router registration check: every extracted router route is registered once.
- Runtime singleton check: WhatsApp, store, Calendly, Redis and RQ objects each
  have one module-level initializer and share the same identity in consumers.
- Import side-effect check: the scheduler is not running after module import.
- HTTP smoke checks:
  - `GET /` → 200
  - `GET /health` → 200
  - `GET /webhook` without verification parameters → 400
  - `GET /api/leads` without API key → 403

## Compatibility risks

1. HTTP URLs, methods, request schemas, response schemas, route order, status
   behavior, dependencies, and rate limits are unchanged and verified.
2. Direct Python imports such as `from main import receive_message` are no
   longer supported; handlers now live under `app.api.routers`. No such imports
   were found in the application code, but external/debug code outside the
   repository could theoretically depend on them.
3. Runtime singletons were relocated to `app/api/runtime.py`. They are still
   created once, in the same startup order, with the same classes and config.
4. The existing `TestClient` smoke path is blocked by the repository's current
   Starlette/httpx version mismatch (`Client.__init__()` rejects `app=`).
   Verification therefore used a real local Uvicorn process instead. This
   dependency mismatch existed independently of this refactor.
5. During the initial verification attempt on 2026-07-26, broad legacy test
   discovery imported side-effectful integration scripts and created an
   Airtable test lead with phone `9999999888`. The external record was not
   deleted or otherwise modified. The resumed verification used only static,
   offline checks and a local Uvicorn smoke test with all provider credentials
   explicitly disabled.
6. `tests/stress_test.py`, `tests/verify_lead.py`, and other scripts that access
   live providers are intentionally excluded from this refactor gate. They are
   not isolated unit tests and can mutate or inspect external data.

## Phase boundary

Phase 1.1 stops here. No service extraction, behavior change, database change,
AI change, provider change, queue change, or new feature is included.
