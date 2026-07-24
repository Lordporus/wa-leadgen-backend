# Backend Folder Restructuring Plan

## Background

The backend is a FastAPI + Alembic + Supabase/Postgres service deployed on Render, with a self-hosted MCP proxy (`codebase-memory-mcp`). The codebase evolved incrementally across 8+ phases (Airtable → dual-write → Postgres, multi-tenant, email, RAG, billing), leaving **94 files** flat in the root — a mix of production app code, one-off scripts, debug tools, and test files all at the same level. This plan reorganises them into a clean, professional structure without breaking Render's start command, Alembic, or any import chain.

---

## 1. Proposed Folder Tree

```
backend/
├── main.py                          ← STAYS (Render: uvicorn main:app)
├── worker.py                        ← STAYS (Render worker startCommand)
├── requirements.txt                 ← STAYS
├── alembic.ini                      ← STAYS (Alembic CWD assumption)
├── render.yaml                      ← STAYS
├── .env                             ← STAYS
├── .gitignore                       ← STAYS
├── .mcp.json                        ← STAYS (MCP proxy config)
├── AGENTS.md                        ← STAYS (AI tool config)
├── GEMINI.md                        ← STAYS (AI tool config)
├── CLAUDE.md                        ← STAYS (AI tool config)
│
├── alembic/                         ← STAYS (Alembic internal)
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│
├── migrations/                      ← STAYS (raw SQL migrations)
│
├── app/                             ← NEW: all importable Python packages
│   │
│   ├── core/                        ← Config, DB engine, ORM models
│   │   ├── __init__.py
│   │   ├── config.py                ← was: config.py
│   │   ├── database.py              ← was: database.py
│   │   └── models.py                ← was: models.py
│   │
│   ├── clients/                     ← Third-party API wrappers
│   │   ├── __init__.py
│   │   ├── airtable_client.py       ← was: airtable_client.py
│   │   ├── whatsapp_client.py       ← was: whatsapp_client.py
│   │   ├── gemini_client.py         ← was: gemini_client.py
│   │   └── calendly_client.py       ← was: calendly_client.py
│   │
│   ├── store/                       ← Data access / migration orchestration
│   │   ├── __init__.py
│   │   ├── store.py                 ← was: store.py
│   │   ├── db_client.py             ← was: db_client.py
│   │   └── webhook_store.py         ← was: webhook_store.py
│   │
│   ├── services/                    ← Business logic services
│   │   ├── __init__.py
│   │   ├── tenant.py                ← was: tenant.py
│   │   ├── billing.py               ← was: billing.py
│   │   ├── usage.py                 ← was: usage.py
│   │   ├── analytics.py             ← was: analytics.py
│   │   ├── guardrails.py            ← was: guardrails.py
│   │   ├── rag.py                   ← was: rag.py
│   │   ├── ingestion.py             ← was: ingestion.py
│   │   ├── jobs.py                  ← was: jobs.py
│   │   └── scraper.py               ← was: scraper.py
│   │
│   └── email/                       ← Email channel (all E-phase files)
│       ├── __init__.py
│       ├── email_client.py          ← was: email_client.py
│       ├── email_templates.py       ← was: email_templates.py
│       ├── email_validation.py      ← was: email_validation.py
│       ├── email_webhooks.py        ← was: email_webhooks.py
│       ├── email_inbound.py         ← was: email_inbound.py
│       ├── email_campaigns.py       ← was: email_campaigns.py
│       └── email_ai.py              ← was: email_ai.py
│
├── scripts/                         ← One-off operational / admin scripts
│   ├── onboard_client.py            ← was: onboard_client.py
│   ├── scraper_run.py               ← was: send_initial_outreach.py (rename for clarity)
│   ├── send_initial_outreach.py     ← was: send_initial_outreach.py
│   ├── migrate_airtable_to_postgres.py ← was: migrate_airtable_to_postgres.py
│   ├── run_004_migration.py         ← was: run_004_migration.py
│   ├── run_005_migration.py         ← was: run_005_migration.py
│   ├── hygiene.py                   ← was: hygiene.py
│   └── print_airtable_keys.py       ← was: print_airtable_keys.py
│
├── tests/                           ← All test / verification scripts
│   ├── test_airtable.py
│   ├── test_airtable_error.py
│   ├── test_analytics.py
│   ├── test_analytics_2.py
│   ├── test_calendly_sync.py
│   ├── test_gemini.py
│   ├── test_idempotency.py
│   ├── test_parse.py
│   ├── test_parse2.py
│   ├── test_rate_limit.py
│   ├── test_store.py
│   ├── test_wa_error.py
│   ├── test_webhook.py
│   ├── test_webhook2.py
│   ├── test_webhook3.py
│   ├── test_z.py
│   ├── local_test.py
│   ├── stress_test.py
│   └── verify_lead.py
│
├── debug/                           ← Debug/profiling/ad-hoc tooling
│   ├── profile_webhook.py
│   ├── check_db.py
│   ├── check_leads.py
│   ├── query_wamid.py
│   ├── parse_logs.py
│   ├── send_prod_webhook.py
│   ├── full_prod_verification.py
│   ├── prod_verification.py
│   ├── verify2.py
│   └── patch_main.py / patch_rate_limit.py / patch_response.py
│
└── docs/                            ← Documentation (already partially exists)
    ├── README.md                    ← was: README.md
    ├── PRODUCTION_READY.md
    ├── RELEASE_NOTES.md
    ├── REMAINING_DEVELOPMENT.md
    ├── system_walkthrough.MD
    ├── v2_upgrade.md
    └── (existing docs/ subdirectory contents)
```

---

## 2. File-by-File Mapping Table

### 2a. Files That Move Into `app/core/`

| Current Path | New Path |
|---|---|
| `config.py` | `app/core/config.py` |
| `database.py` | `app/core/database.py` |
| `models.py` | `app/core/models.py` |

### 2b. Files That Move Into `app/clients/`

| Current Path | New Path |
|---|---|
| `airtable_client.py` | `app/clients/airtable_client.py` |
| `whatsapp_client.py` | `app/clients/whatsapp_client.py` |
| `gemini_client.py` | `app/clients/gemini_client.py` |
| `calendly_client.py` | `app/clients/calendly_client.py` |

### 2c. Files That Move Into `app/store/`

| Current Path | New Path |
|---|---|
| `store.py` | `app/store/store.py` |
| `db_client.py` | `app/store/db_client.py` |
| `webhook_store.py` | `app/store/webhook_store.py` |

### 2d. Files That Move Into `app/services/`

| Current Path | New Path |
|---|---|
| `tenant.py` | `app/services/tenant.py` |
| `billing.py` | `app/services/billing.py` |
| `usage.py` | `app/services/usage.py` |
| `analytics.py` | `app/services/analytics.py` |
| `guardrails.py` | `app/services/guardrails.py` |
| `rag.py` | `app/services/rag.py` |
| `ingestion.py` | `app/services/ingestion.py` |
| `jobs.py` | `app/services/jobs.py` |
| `scraper.py` | `app/services/scraper.py` |

### 2e. Files That Move Into `app/email/`

| Current Path | New Path |
|---|---|
| `email_client.py` | `app/email/email_client.py` |
| `email_templates.py` | `app/email/email_templates.py` |
| `email_validation.py` | `app/email/email_validation.py` |
| `email_webhooks.py` | `app/email/email_webhooks.py` |
| `email_inbound.py` | `app/email/email_inbound.py` |
| `email_campaigns.py` | `app/email/email_campaigns.py` |
| `email_ai.py` | `app/email/email_ai.py` |

### 2f. Files That Move Into `scripts/`

| Current Path | New Path |
|---|---|
| `onboard_client.py` | `scripts/onboard_client.py` |
| `send_initial_outreach.py` | `scripts/send_initial_outreach.py` |
| `migrate_airtable_to_postgres.py` | `scripts/migrate_airtable_to_postgres.py` |
| `run_004_migration.py` | `scripts/run_004_migration.py` |
| `run_005_migration.py` | `scripts/run_005_migration.py` |
| `hygiene.py` | `scripts/hygiene.py` |
| `print_airtable_keys.py` | `scripts/print_airtable_keys.py` |

### 2g. Files That Move Into `tests/`

| Current Path | New Path |
|---|---|
| `test_airtable.py` | `tests/test_airtable.py` |
| `test_airtable_error.py` | `tests/test_airtable_error.py` |
| `test_analytics.py` | `tests/test_analytics.py` |
| `test_analytics_2.py` | `tests/test_analytics_2.py` |
| `test_calendly_sync.py` | `tests/test_calendly_sync.py` |
| `test_gemini.py` | `tests/test_gemini.py` |
| `test_idempotency.py` | `tests/test_idempotency.py` |
| `test_parse.py` | `tests/test_parse.py` |
| `test_parse2.py` | `tests/test_parse2.py` |
| `test_rate_limit.py` | `tests/test_rate_limit.py` |
| `test_store.py` | `tests/test_store.py` |
| `test_wa_error.py` | `tests/test_wa_error.py` |
| `test_webhook.py` | `tests/test_webhook.py` |
| `test_webhook2.py` | `tests/test_webhook2.py` |
| `test_webhook3.py` | `tests/test_webhook3.py` |
| `test_z.py` | `tests/test_z.py` |
| `local_test.py` | `tests/local_test.py` |
| `stress_test.py` | `tests/stress_test.py` |
| `verify_lead.py` | `tests/verify_lead.py` |

### 2h. Files That Move Into `debug/`

| Current Path | New Path |
|---|---|
| `profile_webhook.py` | `debug/profile_webhook.py` |
| `check_db.py` | `debug/check_db.py` |
| `check_leads.py` | `debug/check_leads.py` |
| `query_wamid.py` | `debug/query_wamid.py` |
| `parse_logs.py` | `debug/parse_logs.py` |
| `send_prod_webhook.py` | `debug/send_prod_webhook.py` |
| `full_prod_verification.py` | `debug/full_prod_verification.py` |
| `prod_verification.py` | `debug/prod_verification.py` |
| `verify2.py` | `debug/verify2.py` |
| `patch_main.py` | `debug/patch_main.py` |
| `patch_rate_limit.py` | `debug/patch_rate_limit.py` |
| `patch_response.py` | `debug/patch_response.py` |

### 2i. Files That Move Into `docs/`

| Current Path | New Path |
|---|---|
| `README.md` | `docs/README.md` *(or keep at root — convention varies)* |
| `PRODUCTION_READY.md` | `docs/PRODUCTION_READY.md` |
| `RELEASE_NOTES.md` | `docs/RELEASE_NOTES.md` |
| `REMAINING_DEVELOPMENT.md` | `docs/REMAINING_DEVELOPMENT.md` |
| `system_walkthrough.MD` | `docs/system_walkthrough.md` |
| `v2_upgrade.md` | `docs/v2_upgrade.md` |
| `THIRD_PARTY_NOTICES.md` | `docs/THIRD_PARTY_NOTICES.md` |

---

## 3. Files That Must NOT Be Moved

> [!CAUTION]
> Moving any of these will immediately break Render deployments or Alembic. Do not touch them.

| File | Reason |
|---|---|
| `main.py` | Render `startCommand: "uvicorn main:app"` references this exact filename and module name from the CWD. Moving it to `app/main.py` requires changing render.yaml startCommand. |
| `worker.py` | If a Render Worker service is configured, its start command points to `python worker.py`. Same constraint as above. |
| `alembic.ini` | Alembic CLI (`alembic upgrade head`) searches for `alembic.ini` in the CWD. `script_location = alembic` inside it is a relative path. |
| `alembic/` (entire dir) | `alembic.ini` `script_location = alembic`. `alembic/env.py` uses `sys.path.insert(0, Path(__file__).resolve().parents[1])` which resolves to the directory containing `alembic/`, i.e. backend root. Moving would break path resolution. |
| `migrations/` | Raw SQL files; referenced in documentation and run manually via psql. No code imports from here. |
| `.env` | dotenv `load_dotenv()` in `config.py` and several scripts use the default path `.env` relative to CWD. |
| `.gitignore` | Git tooling expects this at repo root. |
| `.mcp.json` | MCP proxy expects this at the directory where `codegraph serve --mcp` is launched (the backend root). |
| `requirements.txt` | Render `buildCommand: "pip install -r requirements.txt"` uses this at CWD. |
| `render.yaml` | Render platform reads this from repo root for IaC deployments. |
| `AGENTS.md` / `GEMINI.md` / `CLAUDE.md` | AI tool configs discovered by convention from the project root. |

---

## 4. Import Update Audit

This is the highest-risk part. Every file that moves into a subpackage must update its bare-name imports to package-qualified paths. **`main.py` itself will need the most changes**, because it imports from ALL modules.

> [!IMPORTANT]
> Every file in `app/` needs an `__init__.py` created. The pattern for every internal import changes from `from foo import Bar` to `from app.foo.bar import Bar`.

### 4a. `main.py` (root — stays in place, imports updated to point into `app/`)

```python
# BEFORE
from config import (WHATSAPP_VERIFY_TOKEN, ...)
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from calendly_client import CalendlyClient
from store import get_store, get_primary_store, get_secondary_store
from webhook_store import WebhookStore
import tenant
from database import SessionLocal
from models import Client, PipelineStage, ...
from email_client import email_client, EmailSendError
from email_templates import apply_merge_fields, ...
from email_webhooks import handle_resend_event, ...
from email_validation import validate_lead_email
from email_ai import generate_email_draft
from usage import check_limit, log_usage
import analytics

# AFTER
from app.core.config import (WHATSAPP_VERIFY_TOKEN, ...)
from app.clients.whatsapp_client import WhatsAppClient
from app.clients.gemini_client import GeminiClient
from app.clients.calendly_client import CalendlyClient
from app.store.store import get_store, get_primary_store, get_secondary_store
from app.store.webhook_store import WebhookStore
from app.services import tenant
from app.core.database import SessionLocal
from app.core.models import Client, PipelineStage, ...
from app.email.email_client import email_client, EmailSendError
from app.email.email_templates import apply_merge_fields, ...
from app.email.email_webhooks import handle_resend_event, ...
from app.email.email_validation import validate_lead_email
from app.email.email_ai import generate_email_draft
from app.services.usage import check_limit, log_usage
from app.services import analytics
```

### 4b. `worker.py` (root — stays in place)

```python
# BEFORE
from config import REDIS_URL

# AFTER
from app.core.config import REDIS_URL
```

### 4c. `app/core/models.py`

```python
# BEFORE
from database import Base

# AFTER
from app.core.database import Base
```

### 4d. `app/store/store.py`

```python
# BEFORE
from config import MIGRATION_MODE, DATABASE_URL
from airtable_client import AirtableClient
from database import init_engine

# AFTER
from app.core.config import MIGRATION_MODE, DATABASE_URL
from app.clients.airtable_client import AirtableClient
from app.core.database import init_engine
# (lazy imports inside get_store also need updating:)
# from db_client import DatabaseClient → from app.store.db_client import DatabaseClient
```

### 4e. `app/store/db_client.py`

```python
# BEFORE
from database import SessionLocal, is_configured
from models import Lead, Message, Client

# AFTER
from app.core.database import SessionLocal, is_configured
from app.core.models import Lead, Message, Client
```

### 4f. `app/clients/airtable_client.py`

```python
# BEFORE
from config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

# AFTER
from app.core.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
```

### 4g. `app/clients/whatsapp_client.py`

```python
# BEFORE
from config import (WHATSAPP_ACCESS_TOKEN, ...)

# AFTER
from app.core.config import (WHATSAPP_ACCESS_TOKEN, ...)
```

### 4h. `app/clients/gemini_client.py`

```python
# BEFORE
from config import (GEMINI_API_KEY, NINEROUTER_API_KEY, ...)

# AFTER
from app.core.config import (GEMINI_API_KEY, NINEROUTER_API_KEY, ...)
```

### 4i. `app/services/tenant.py`

```python
# BEFORE
from database import SessionLocal, is_configured
from models import Client, PipelineStage

# AFTER
from app.core.database import SessionLocal, is_configured
from app.core.models import Client, PipelineStage
# (also: from gemini_client import GeminiClient → from app.clients.gemini_client import GeminiClient)
```

### 4j. `app/services/billing.py`

```python
# BEFORE
from config import (RAZORPAY_KEY_ID, ...)
from database import SessionLocal, is_configured
from models import Client

# AFTER
from app.core.config import (RAZORPAY_KEY_ID, ...)
from app.core.database import SessionLocal, is_configured
from app.core.models import Client
```

### 4k. `app/services/usage.py`

```python
# BEFORE
from database import SessionLocal, is_configured
from models import UsageEvent, Client

# AFTER
from app.core.database import SessionLocal, is_configured
from app.core.models import UsageEvent, Client
```

### 4l. `app/services/analytics.py`

```python
# BEFORE
from database import SessionLocal, is_configured
from models import Lead, Message, DailyStat
import tenant

# AFTER
from app.core.database import SessionLocal, is_configured
from app.core.models import Lead, Message, DailyStat
from app.services import tenant
```

### 4m. `app/services/rag.py`

```python
# BEFORE
from database import SessionLocal, is_configured
from models import Document
from ingestion import embed_text

# AFTER
from app.core.database import SessionLocal, is_configured
from app.core.models import Document
from app.services.ingestion import embed_text
```

### 4n. `app/services/ingestion.py`

```python
# BEFORE
from config import GEMINI_API_KEY
from database import SessionLocal
from models import Document
from usage import log_usage, estimate_tokens, COST_PER_1K_EMBEDDING_TOKENS

# AFTER
from app.core.config import GEMINI_API_KEY
from app.core.database import SessionLocal
from app.core.models import Document
from app.services.usage import log_usage, estimate_tokens, COST_PER_1K_EMBEDDING_TOKENS
```

### 4o. `app/services/guardrails.py`

No internal imports — only `re` and `logging`. **No import changes needed.**

### 4p. `app/services/jobs.py`

```python
# BEFORE
from config import (LORD_PHONE_NUMBER, BLOCKED_NUMBERS, ...)
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from store import get_store
from guardrails import scan_input, redact_pii, score_confidence, CONFIDENCE_THRESHOLD
from database import SessionLocal
from models import Lead, Client
from rag import retrieve_context
from usage import log_usage, estimate_tokens, check_limit, ...
import tenant

# AFTER
from app.core.config import (LORD_PHONE_NUMBER, BLOCKED_NUMBERS, ...)
from app.clients.whatsapp_client import WhatsAppClient
from app.clients.gemini_client import GeminiClient
from app.store.store import get_store
from app.services.guardrails import scan_input, redact_pii, score_confidence, CONFIDENCE_THRESHOLD
from app.core.database import SessionLocal
from app.core.models import Lead, Client
from app.services.rag import retrieve_context
from app.services.usage import log_usage, estimate_tokens, check_limit, ...
from app.services import tenant
```

### 4q. `app/services/scraper.py`

```python
# BEFORE
from config import APIFY_API_TOKEN
from store import get_store

# AFTER
from app.core.config import APIFY_API_TOKEN
from app.store.store import get_store
```

### 4r. `app/email/email_client.py`

```python
# BEFORE
from config import (EMAIL_DAILY_CAP, ...)

# AFTER
from app.core.config import (EMAIL_DAILY_CAP, ...)
```

### 4s. `app/email/email_templates.py`

```python
# BEFORE
from config import EMAIL_UNSUB_SECRET, JWT_SECRET, PUBLIC_API_URL

# AFTER
from app.core.config import EMAIL_UNSUB_SECRET, JWT_SECRET, PUBLIC_API_URL
```

### 4t. `app/email/email_validation.py`

```python
# BEFORE
from email_templates import is_valid_email_format

# AFTER
from app.email.email_templates import is_valid_email_format
```

### 4u. `app/email/email_webhooks.py`

```python
# BEFORE
from config import RESEND_WEBHOOK_SECRET
from database import SessionLocal, is_configured
from models import EmailSuppression, Lead, Message

# AFTER
from app.core.config import RESEND_WEBHOOK_SECRET
from app.core.database import SessionLocal, is_configured
from app.core.models import EmailSuppression, Lead, Message
```

### 4v. `app/email/email_inbound.py`

```python
# BEFORE
from config import (EMAIL_AI_AUTO_REPLY, EMAIL_DEFAULT_FROM_ADDRESS)
from database import SessionLocal, is_configured
from email_client import EmailClient, EmailSendError, email_client
from email_templates import build_unsubscribe_url, wrap_email_bodies
from guardrails import (CONFIDENCE_THRESHOLD, redact_pii, scan_input, score_confidence)
from models import Client, EmailSuppression, Lead, Message

# AFTER
from app.core.config import (EMAIL_AI_AUTO_REPLY, EMAIL_DEFAULT_FROM_ADDRESS)
from app.core.database import SessionLocal, is_configured
from app.email.email_client import EmailClient, EmailSendError, email_client
from app.email.email_templates import build_unsubscribe_url, wrap_email_bodies
from app.services.guardrails import (CONFIDENCE_THRESHOLD, redact_pii, scan_input, score_confidence)
from app.core.models import Client, EmailSuppression, Lead, Message
```

### 4w. `app/email/email_campaigns.py`

```python
# BEFORE
from config import EMAIL_DEFAULT_FROM_ADDRESS, EMAIL_DEFAULT_FROM_NAME
from database import SessionLocal, is_configured
from email_client import EmailSendError, email_client
from email_templates import (apply_merge_fields, build_unsubscribe_url, wrap_email_bodies)
from email_validation import validate_lead_email
from models import (Client, EmailCampaign, ...)
from usage import check_limit, log_usage

# AFTER
from app.core.config import EMAIL_DEFAULT_FROM_ADDRESS, EMAIL_DEFAULT_FROM_NAME
from app.core.database import SessionLocal, is_configured
from app.email.email_client import EmailSendError, email_client
from app.email.email_templates import (apply_merge_fields, build_unsubscribe_url, wrap_email_bodies)
from app.email.email_validation import validate_lead_email
from app.core.models import (Client, EmailCampaign, ...)
from app.services.usage import check_limit, log_usage
```

### 4x. `app/email/email_ai.py`

```python
# BEFORE
from guardrails import (CONFIDENCE_THRESHOLD, redact_pii, scan_input, score_confidence)
from usage import estimate_tokens

# AFTER
from app.services.guardrails import (CONFIDENCE_THRESHOLD, redact_pii, scan_input, score_confidence)
from app.services.usage import estimate_tokens
```

### 4y. `scripts/` — All scripts that import internal modules

All scripts call bare-name imports and also call `load_dotenv()` + `init_engine()` manually. After the move they need `sys.path` manipulation OR you add a `pyproject.toml`/`PYTHONPATH` configuration. **Safest approach**: add at the top of every script:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
```

Then update all imports with the same `app.*` prefix as above. Scripts affected:

- `scripts/onboard_client.py` — imports `config`, `database`, `models`
- `scripts/migrate_airtable_to_postgres.py` — imports `airtable_client`, `database`, `models`
- `scripts/hygiene.py` — imports `airtable_client`
- `scripts/send_initial_outreach.py` — imports `airtable_client`, `whatsapp_client`
- `scripts/run_004_migration.py` — likely imports `database`, `models`
- `scripts/run_005_migration.py` — likely imports `database`, `models`
- `scripts/print_airtable_keys.py` — imports `airtable_client`

### 4z. `alembic/env.py` — Critical: DO NOT CHANGE the sys.path line

```python
# This line:
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ...resolves to backend/ root. This is correct and must stay.

# BUT these imports will need updating because the files move:
# BEFORE
from config import DATABASE_URL
from database import Base
import models

# AFTER
from app.core.config import DATABASE_URL
from app.core.database import Base
import app.core.models   # ← registers all tables on Base.metadata
```

> [!WARNING]
> The `alembic/env.py` change is the single most critical step. If the import of `Base` or `models` breaks, `alembic upgrade head` will silently produce wrong migrations or crash.

---

## 5. Risks in This Specific Codebase

### Risk 1: `store.py` has lazy imports inside functions (HIGH risk)

Inside `get_store()`, `get_primary_store()`, `get_secondary_store()`:
```python
from db_client import DatabaseClient  # lazy, inside function body
from airtable_client import AirtableClient
```
These lazy imports **will not be caught by a simple grep for top-level imports**. They must be updated to `from app.store.db_client import ...` etc. Failing to do so causes a `ModuleNotFoundError` only at **runtime when the store mode activates**, not at startup — making it hard to catch in testing.

### Risk 2: `alembic/env.py` must see `app.core.models` to register all ORM tables

The line `import models` registers every SQLAlchemy model on `Base.metadata` so Alembic's autogenerate knows what tables exist. After the move, this becomes `import app.core.models`. If you forget this and only import `Base`, Alembic will see an empty metadata and generate a migration that **drops all your tables**. Extremely dangerous.

### Risk 3: Hardcoded Render start command

`render.yaml` line 7: `startCommand: "uvicorn main:app ..."` — this means `main.py` MUST remain at the backend root. If you accidentally move it into `app/`, Render deploys will fail with `ModuleNotFoundError: No module named 'main'`. This is the #1 practical constraint.

### Risk 4: `email_validation.py` imports from `email_templates.py` (intra-email dependency)

Within the email module, `email_validation.py` does `from email_templates import is_valid_email_format`. After moving both to `app/email/`, this becomes `from app.email.email_templates import ...`. Since they're siblings, you could also use a relative import `from .email_templates import ...`. Pick one style and be consistent.

### Risk 5: Circular import risk in `app/services/`

The current code has a subtle chain: `store.py` → `airtable_client.py` → `config.py`, and `store.py` also → `database.py` (init_engine at import time). After restructuring, `app/store/store.py` → `app/clients/airtable_client.py` → `app/core/config.py`. This is fine. But `app/services/jobs.py` → `app/store/store.py` → `app/core/database.py` → (no back-edges). No circular dependency emerges, but it should be validated after each phase.

### Risk 6: `calendly_client.py` doesn't import from `config.py`

It reads `CALENDLY_API_TOKEN` directly from `os.getenv()` at module level — **bypasses `config.py` entirely**. This is already slightly inconsistent, but it means no import change is needed for this file's env-reading. Just the file move itself.

### Risk 7: Debug/test scripts have `load_dotenv()` and `os.environ` overrides at top

Scripts like `profile_webhook.py` do `os.environ['MIGRATION_MODE'] = 'dual'` before importing `store`. After the move to `debug/`, the relative CWD changes if you run `python debug/profile_webhook.py` from the backend root (dotenv `.env` is still found) vs `cd debug && python profile_webhook.py` (dotenv fails). Ensure scripts are always run from the `backend/` root.

### Risk 8: `main.py` imports `billing.py` indirectly via route handlers

Check if `main.py` directly imports `billing`. If yes, add `from app.services.billing import ...` to the import update list. (The grep shows `billing` is used in Razorpay webhook routes in main.)

### Risk 9: `hygiene.py` is both a utility script and importable

It imports `from airtable_client import AirtableClient`. Once moved to `scripts/`, it's no longer on the Python path, so it needs the `sys.path.insert` header to work.

### Risk 10: `__pycache__` directories

Moving Python files to new locations means old `__pycache__` directories at the root will contain stale `.pyc` files that can cause confusing import errors. **All `__pycache__` directories at the root must be deleted** after the move.

---

## 6. Execution Plan — Phased, Testable Steps

> [!NOTE]
> Each phase ends with a verification gate. Do not proceed to the next phase until the gate passes.

### Phase 0: Preparation (no file changes)

1. Create a git branch: `git checkout -b refactor/folder-structure`
2. Run `uvicorn main:app` locally and confirm it starts without errors → baseline.
3. Run `alembic check` (or `alembic upgrade head --sql`) to confirm migrations are detected.
4. Take a snapshot of the current `sys.modules` or just note the working state.

**Gate**: App starts, Alembic sees all 12+ migration files.

---

### Phase 1: Create package skeleton (no file moves yet)

1. Create `app/__init__.py` (empty)
2. Create `app/core/__init__.py` (empty)
3. Create `app/clients/__init__.py` (empty)
4. Create `app/store/__init__.py` (empty)
5. Create `app/services/__init__.py` (empty)
6. Create `app/email/__init__.py` (empty)
7. Create `scripts/`, `tests/`, `debug/` directories (touch a `.gitkeep` in each)

**Gate**: Python can `import app` without error. No existing code is affected.

---

### Phase 2: Move `app/core/` files (foundation layer — no app-level imports of these yet)

Files: `config.py` → `app/core/config.py`, `database.py` → `app/core/database.py`, `models.py` → `app/core/models.py`

Steps:
1. Copy files to new locations (use git mv to preserve history).
2. Update `app/core/models.py`: change `from database import Base` → `from app.core.database import Base`.
3. Leave original files at root **temporarily as shims** that re-export:
   ```python
   # config.py (shim — delete after all callers updated)
   from app.core.config import *
   ```
4. Confirm `uvicorn main:app` still starts (shims make old imports work).

**Gate**: Server starts, `alembic check` still works, no import errors.

---

### Phase 3: Update `alembic/env.py`

This is done early because it's isolated and high-risk-if-skipped.

1. Open `alembic/env.py`
2. Change:
   ```python
   from config import DATABASE_URL
   from database import Base
   import models
   ```
   To:
   ```python
   from app.core.config import DATABASE_URL
   from app.core.database import Base
   import app.core.models  # registers all ORM models
   ```
3. Run `alembic current` — must show the current revision without error.
4. Run `alembic check` — must show "No new upgrade operations detected" (no phantom drops).

**Gate**: `alembic current` and `alembic check` both pass.

---

### Phase 4: Move `app/clients/` files and update their imports

Files: `airtable_client.py`, `whatsapp_client.py`, `gemini_client.py`, `calendly_client.py`

Steps for each:
1. git mv to `app/clients/<filename>.py`
2. Update internal imports (all import from `config` → `app.core.config`)
3. Keep root-level shim if `main.py` still uses old-style import (shim: `from app.clients.x import *`)

**Gate**: `python -c "from app.clients.whatsapp_client import WhatsAppClient"` succeeds.

---

### Phase 5: Move `app/store/` files and update their imports

Files: `store.py`, `db_client.py`, `webhook_store.py`

Steps:
1. git mv to `app/store/`
2. In `app/store/store.py`: update ALL imports including the **lazy imports inside function bodies**:
   ```python
   from app.clients.airtable_client import AirtableClient  # top-level
   # inside get_store():
   from app.store.db_client import DatabaseClient          # lazy — DO NOT MISS
   ```
3. In `app/store/db_client.py`: update to `app.core.database` and `app.core.models`.

**Gate**: `python -c "from app.store.store import get_store"` succeeds without Postgres connection.

---

### Phase 6: Move `app/services/` files and update their imports

Files: `tenant.py`, `billing.py`, `usage.py`, `analytics.py`, `guardrails.py`, `rag.py`, `ingestion.py`, `jobs.py`, `scraper.py`

Steps:
1. Move in dependency order (leaf nodes first): `guardrails` (no internal deps) → `usage` → `ingestion` → `rag` → `tenant` → `analytics` → `billing` → `jobs` → `scraper`.
2. Update all imports per section 4 above.

**Gate**: `python -c "from app.services.jobs import process_webhook_message"` succeeds.

---

### Phase 7: Move `app/email/` files and update their imports

Files: all 7 `email_*.py` files

Steps:
1. Move in dependency order: `email_templates` → `email_validation` → `email_client` → `email_webhooks` → `email_ai` → `email_inbound` → `email_campaigns`.
2. Update all imports per section 4 above.

**Gate**: `python -c "from app.email.email_client import email_client"` succeeds.

---

### Phase 8: Update `main.py` and `worker.py` imports

This is the largest single-file change. `main.py` has ~20 imports to update.

Steps:
1. Update all imports in `main.py` per section 4a above.
2. Update `worker.py` per section 4b above.
3. **Delete all root-level shim files** created in phases 2–7.
4. Delete all `__pycache__` directories at the root.

**Gate**: `uvicorn main:app --reload` starts clean with no import errors. All shims gone.

---

### Phase 9: Move scripts, tests, debug files

Files: everything in sections 2f, 2g, 2h.

Steps:
1. git mv each file to its new location.
2. Add `sys.path.insert` header to all scripts that import internal modules.
3. Update imports in scripts from bare names to `app.*` qualified names.

**Gate**: `python scripts/onboard_client.py --help` runs without import errors (it will exit early without a real DB, but must not crash on import).

---

### Phase 10: Move docs and clean up binary/temp files

1. git mv docs to `docs/` (or leave README.md at root — convention varies, both are fine).
2. Delete: `backend_response.txt`, `body.txt`, `headers.txt`, `last_test_phone.txt`, `openapi.json` (regenerate via `/openapi.json` endpoint), `opencode.jsonc` (dev tooling config — move to `.vscode/` or keep at root), `whatsapp_leads_dashboard.html` (move to `docs/` or `debug/`).
3. Assess `codebase-memory-mcp.exe` and `codebase-memory-mcp.zip` — these are large binaries (273 MB). They should be in `.gitignore` and never committed. Confirm they're ignored, then consider deleting locally.
4. `install.ps1` — development setup script, move to `scripts/` or keep at root.

**Gate**: `git status` shows a clean, well-organized tree. Run the full server + alembic check one final time.

---

### Phase 11: Update PYTHONPATH for Render (if needed)

Since `main.py` stays at the root and does `from app.core.config import ...`, Python must be able to find the `app` package. Because `main.py` is in `backend/` and `app/` is also in `backend/`, this **works automatically** — Python's import system adds the script's directory to `sys.path`. **No PYTHONPATH change is needed on Render.**

**Gate**: Deploy to Render staging, confirm service starts and `/health` returns 200.

---

## Open Questions for Your Review

> [!IMPORTANT]
> Please decide on these before I execute:

1. **`README.md` location**: Keep at `backend/` root (standard practice for GitHub) or move into `docs/`? Most repos keep `README.md` at root.

2. **`scraper.py` placement**: It's currently used both as a library (imported by nothing in production — it's a standalone pipeline runner) and as a script. Should it live in `app/services/scraper.py` (importable) or `scripts/scraper.py` (one-off runner)?

3. **`install.ps1`**: Keep at root (developer convenience) or move to `scripts/`?

4. **Shim deletion timing**: Should shims (root-level re-exports) remain for 1-2 deployment cycles to enable rollback, or delete immediately after `main.py` is updated?

5. **`alembic/env.py` vs keeping bare imports**: Some teams prefer the env.py to use bare names and add the `app/` directory itself to sys.path. Would you prefer that simpler but less explicit approach?
