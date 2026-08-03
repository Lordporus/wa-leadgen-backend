# Phase 11 Azure release and recovery runbook

This runbook is the approval boundary for the existing Azure VPS production
deployment. It does not authorize an infrastructure redesign, migration,
secret change, DNS/TLS change, or `MIGRATION_MODE` cutover. The deployed
Compose topology, `/opt/qualify/backend` backend root,
`/opt/qualify/frontend` frontend root, and `/etc/qualify` runtime files remain
the production contract. The shared Compose file remains backend-owned at
`/opt/qualify/backend/deploy/docker-compose.production.yml`.

## Required release record

Record this non-secret manifest before approving a production deployment:

- backend commit SHA and immutable GHCR image reference;
- frontend commit SHA and immutable GHCR image reference, when changed;
- SHA-256 of `deploy/docker-compose.production.yml`;
- repository Alembic head and expected database revision;
- names of changed configuration keys and non-secret flag values only;
- successful backend/frontend CI run IDs for the exact release commits;
- staging or equivalent pre-production smoke evidence and reviewer;
- migration approval ID when the database revision changes;
- production deployment approval ID, release operator, incident owner, and
  rollback owner;
- post-release `/ready`, `/health`, queue-consumer, running-image-revision, and
  migration-revision evidence.

Never copy secret values, access tokens, connection strings, private keys, or
customer content into a release record or workflow summary. GitHub production
environment protection must require an explicit reviewer approval. If that
protection is absent, the workflow trigger is not production authorization.

## Migration and compatibility gate

Every schema change follows expand -> backfill -> contract across separately
deployable releases. A contract or destructive-risk migration must not remove
the rollback window for the currently running API or worker. Applied revisions
are never edited. Prefer a reviewed forward fix; use downgrade or restore only
when that exact recovery path has been tested and approved.

Only `.github/workflows/release-migration.yml` may apply a release migration.
It checks out the reviewed full commit SHA, proves that SHA belongs to `main`
and has a successful push-triggered Backend CI run, requires the expected
current and target revisions, verified backup/recovery evidence, an approval
identifier, and a PostgreSQL advisory lock. The migration actor validates the
database revision again after Alembic completes. Web and worker startup and the
Azure deployment scripts never run migrations.

Before application rollout, record a compatibility decision for the current
database revision, candidate API, previous worker, and candidate worker. The
backend Azure script replaces and verifies the API first while the previous
worker remains active, then replaces and verifies the worker from the same
immutable backend image. A readiness, revision, or queue-consumer failure
causes verified application rollback.

## Pre-production evidence

Use an isolated staging environment when it is available. If production-like
rehearsal is performed by another approved mechanism, record why it is
equivalent and every material difference. It must never share a production
database, Redis namespace, WhatsApp credentials, or outbound destination.

1. Pin the release commits, images, Compose digest, and non-secret config
   manifest; keep `MIGRATION_MODE=dual`.
2. Verify a recoverable backup and record the restore owner and timestamp.
3. If a revision changes, run only the approval-gated migration actor and
   capture its preflight and post-validation output.
4. Verify `/ready`, `/health`, one active queue consumer, inbound webhook queue
   durability, and outbound-disabled smoke tests.
5. Rehearse a worker rollback to the pinned compatible image.
6. Rehearse an additive-schema application rollback that retains the schema,
   or the documented forward-fix recovery path.
7. Obtain reviewer sign-off. Missing evidence is a production stop condition,
   not permission to weaken a check.

## Azure production promotion

1. Reconfirm the exact CI-tested commits and immutable image references. A
   rebuilt, branch-only, abbreviated, or non-main SHA is not releasable.
2. Reconfirm backup/recovery evidence, expected Alembic revision, release
   record, rollback owner, and production approval. Run the migration workflow
   first only when the reviewed release changes the revision.
3. Confirm the migration actor reports the expected post-migration revision.
   Stop on an unknown revision, multiple Alembic heads, or failed validation.
4. Approve the protected Azure production deployment. Backend and frontend
   workflows share the existing VPS lock and deploy only the selected component.
5. For backend, confirm candidate API readiness before the worker changes, then
   confirm the candidate worker revision, `/ready`, `/health`, and consumer
   count. For frontend, confirm its revision and local health response.
6. Attach workflow summaries and health evidence to the release record. Keep
   production in backward-compatible `dual` mode unless a separate phase and
   approval explicitly authorize a cutover.

## Stop and recover

Stop for missing review evidence, an unexpected commit or image, unknown
migration state, incompatible API/worker/schema versions, failed readiness,
missing queue consumer, material pre-production differences, or absent backup
and approval records. Do not continue by disabling checks.

For an incident, disable outbound WhatsApp using the approved kill switch while
preserving inbound events, durable queue data, and unresolved outbox intents.
The Azure deployment script restores the preceding image descriptor and verifies
container revisions and health. Keep additive schema changes in place and use a
reviewed forward fix. Restore a database only from verified backup under
incident control. Resume outbound traffic only after the incident, migration
(when relevant), and deployment approvers record successful recovery evidence.
