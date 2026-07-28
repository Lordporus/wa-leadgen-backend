# WhatsApp lead-ID and store contract

This document records the Phase 1 contract for WhatsApp CRM routes and jobs.
It does not change `MIGRATION_MODE` or either data source.

## Public lead IDs

| Mode | ID emitted by list/detail | Canonical lookup |
| --- | --- | --- |
| `airtable` | Airtable record ID string (`rec...`) | Tenant-owned Airtable record |
| `dual` | Airtable record ID string (`rec...`) | Airtable read-primary record; normalized messages resolve to the tenant-scoped Postgres lead |
| `postgres` | Postgres integer serialized as a string | Tenant-scoped Postgres primary key |

List, detail, messages, stage update, takeover, release, and manual WhatsApp
send must accept the canonical ID emitted in the active mode. Responses retain
their existing shapes.

## Temporary legacy compatibility

`airtable` and `dual` modes temporarily accept a cached numeric Postgres lead
ID. Resolution is deterministic and tenant scoped:

1. Find `(client_id, Postgres lead ID)`.
2. Use that row's exact phone value to find the record in the configured
   tenant's Airtable table.
3. Reject the request if either scoped lookup fails.

Successful use emits the structured `legacy_lead_id_resolved` event without a
phone number or message body. Set `LEGACY_LEAD_ID_COMPAT_ENABLED=false` to
disable only this legacy resolver; canonical IDs continue to work.

Remove the compatibility path after dashboard caches have expired and
production telemetry shows no successful legacy resolutions for one complete
release observation window.

## Tenant-scoped store protocol

Every WhatsApp store read and write carries `client_id`, including lead
creation, stage/status changes, message append/status updates, lead enrichment,
and scoring. Missing tenant context fails closed. Dual mode forwards the same
tenant ID to Airtable and Postgres; Postgres shadow-write failures remain
contained and are logged without phone numbers or message bodies.

The configured Airtable table belongs to exactly one `CLIENT_ID`, so a request
for another tenant is rejected before any provider request.

Postgres enforces phone uniqueness within a tenant through
`uq_leads_client_phone`. The same normalized phone may belong to different
tenants, while a duplicate within one tenant is rejected. Applying migration
`0013` requires explicit migration approval.

## Rollback

Disable `LEGACY_LEAD_ID_COMPAT_ENABLED` to remove numeric compatibility
translation. If the canonical resolver itself must be rolled back, restore the
previous response serialization adapter while retaining Phase 1 tests and
structured compatibility logs for diagnosis.
