# Phase 11 Render release and recovery runbook

This runbook is the approval boundary for a WhatsApp release. It authorizes no
automatic deployment, migration, secret change, or `MIGRATION_MODE` cutover.

## Release record and compatibility

Before staging, record the immutable commit SHA, Render service/image IDs,
configuration manifest (environment variable names and non-secret flag values
only), repository Alembic head, expected database revision, reviewer, release
operator, rollback owner, and approval IDs. Confirm every schema change uses
expand -> backfill -> contract across separately deployable releases. Do not
combine a contract/destructive migration with a compatible web or worker
rollback window. Applied revisions are never edited. Prefer a tested forward
fix; downgrade only where the migration explicitly supports it and recovery is
approved.

## Staging promotion

1. Confirm staging has distinct database, Redis, Meta resources, and secrets;
   retain the evidence required by `WHATSAPP_STAGING_SAFETY.md`.
2. Pin the recorded commit and configuration manifest; keep `MIGRATION_MODE=dual`.
3. Verify a recoverable staging backup and record its restore owner and time.
4. Set the expected staging Alembic revision and approval ID for this release.
   Run only the manual `release-migration.yml` migration actor. It must report the
   expected current revision and one repository head before upgrade.
5. Roll out web. Require `GET /ready` to return 200, then verify `/health` and
   the staging smoke checklist. The readiness payload must contain no secrets.
6. Roll out the worker separately only after web readiness passes. Verify an
   active consumer and durable queue state through `/health`.
7. Rehearse and record: rollback the staged worker to the pinned release, and
   roll back an additive schema release by reverting application code while
   retaining the schema (or exercise its documented forward-fix path).
8. A reviewer signs staging evidence. Production cannot start without separate
   production deployment approval and, if a revision changes, migration approval.

## Production promotion

1. Reconfirm the same commit/image/config manifest used in staging and the
   expected production revision. Do not promote a rebuilt or unpinned artifact.
2. Confirm fresh production backup/recovery evidence and assign the incident
   owner. Record approval IDs; do not put any secret value in the release record.
3. Run only the manual `release-migration.yml` workflow. It is the sole
   migration actor and must pass revision, backup, approval, and advisory lock
   checks. If it reports an unknown or unexpected revision, stop. Then manually
   deploy only the web release.
4. Confirm `/ready` (200) and `/health` before separately deploying the worker.
   No worker deploy or manual Alembic command may race the migration actor.
5. Record post-release health, queue consumer count, commit/image IDs, actual
   revision, and approval outcome. Production remains in backward-compatible
   `dual` mode.

## Stop and rollback

Stop before production for missing staging evidence, a material environment
difference, unknown revision, failed recovery verification, incompatible
web/worker release, or missing approval. Do not continue by disabling checks.

For a release incident: disable outbound WhatsApp through the approved
configuration control, preserve inbound events, durable queue data, and
unresolved outbound intents, roll web and worker back separately to the pinned
compatible release, and verify health.
Keep additive schema changes in place; use an approved forward-fix migration.
Only an incident-controlled, tested restore may use a verified backup. Resume
outbound traffic only after the incident owner, migration approver (if relevant),
and deployment approver record recovery evidence.
