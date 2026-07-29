# Phase 4 cutover readiness — 2026-07-29

Classification mode: `trusted_postgres_preservation_set`, explicitly authorized by the operator. The 17 PostgreSQL leads are the real-production preservation set. Airtable-only records are visible as development/test records and are not copied by backfill unless an exact Airtable record ID is explicitly approved.

## Real blockers

- One preserved lead has a stage mismatch, a 217-versus-36 legacy-message gap, and a different creation timestamp.
- A second preserved lead has a takeover mismatch and a one-message gap.
- These are data defects: they remain after legacy timestamp, direction/case, and provider-ID comparison normalization.

## Test-data noise

- 44 Airtable-only records are excluded from blocking lead-count discrepancies.
- Five of those records have no usable phone and remain visible as test-data noise.

## Accepted legacy limitations

- Airtable message blobs do not carry provider IDs; provider IDs are audit-only for cross-store comparison.
- Legacy timestamps are compared at second precision after removing timezone/format differences.

## Operator actions

1. Reconcile and repair the two preserved-lead defects in staging only, then rerun the report with zero high-severity findings.
2. Apply migration `0014` and enable durable failure recording in staging; do not apply either in production yet.
3. Run the staging-only backfill dry run. Any test record requires an exact approved Airtable record ID before it can be copied.
4. Complete the PostgreSQL-only staging rehearsal and rollback drill before requesting a production cutover decision.
