# Phase 4: reconciliation and future Postgres cutover

## Deterministic rules

- Tenant ownership never changes automatically. A missing or conflicting tenant is a high-severity stop condition.
- During `dual`, Airtable is canonical for lead, stage, and takeover values. Repair only Postgres, using `(client_id, normalized_phone)`.
- Deduplicate messages by provider ID when available. For Airtable legacy logs, use tenant, normalized phone, timestamp, direction, type, and body fingerprint.
- Preserve source timestamps. Format-only differences are reported; they are never silently rewritten.
- The durable `dual_write_failures` ledger is an operational queue, not a replay trigger. Resolve and replay only through a reviewed staging procedure.

## Staging rehearsal

1. Restore a sanitized production snapshot into isolated staging Postgres and use a dedicated staging Airtable base.
2. Apply additive migrations, then enable `DUAL_WRITE_FAILURE_RECORDING_ENABLED=true` only in staging.
3. Run `python scripts/reconcile_airtable_postgres.py --client-id <tenant>` and require zero high-severity findings.
   Use `--trust-postgres-preservation-set` only with recorded operator approval that Airtable-only rows are development/test data.
4. Use `backfill_airtable_postgres.py` without `--apply` first. It reports additive leads/messages plus stage, takeover, and creation-time repair candidates. `--apply` is rejected unless `APP_ENV=staging`.
5. Run the complete WhatsApp CRM regression suite with `MIGRATION_MODE=postgres`; compare API contracts, messages, stages, takeover, and provider status behavior.
6. Re-run reconciliation after the suite; retain the JSON report and failure-ledger review as approval evidence.

## Production cutover checklist (not authorized by Phase 4)

1. Obtain backup/restore verification, change-window approval, and a pinned release/config manifest.
2. Freeze broad writes, run reconciliation for every configured Airtable tenant, and require zero unexplained high-severity differences and no open dual-write failures.
3. Promote the already-rehearsed staging release only through an approved configuration change.
4. Validate tenant counts, normalized phones, stages, takeover flags, message/provider IDs, and latest timestamps immediately after cutover.
5. Retain Airtable read-only for the approved rollback window. If validation fails, stop outbound automation, restore the pinned dual-compatible release, and repair/replay only from the recorded reconciliation window.
