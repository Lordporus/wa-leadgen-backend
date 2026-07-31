# Render Migration Deployment Gate

Render must start the backend from the `backend/` repository root with:

```sh
uvicorn main:app --host 0.0.0.0 --port $PORT
```

This command is defined in `render.yaml`. Migrations run only from the manual,
approval-gated GitHub `release-migration.yml` workflow before an approved
Render web rollout. The worker never runs Alembic. `DATABASE_URL` must be
available only to the selected GitHub deployment environment.

The migration actor requires an approved release identifier, a verified
backup/recovery confirmation, and the exact current Alembic revision expected
before it connects. It then opens a PostgreSQL transaction and acquires the
application's transaction advisory lock before invoking Alembic on that exact
connection. Concurrent Render instances wait for the same lock. The lock and
all migration DDL share one transaction, which also works with a transaction
pooler. The lock is automatically released on commit, rollback, process death,
or connection loss. After the first instance upgrades the schema, each waiting
instance acquires the lock in turn, verifies that the database is already at
head, and then starts Uvicorn.

Render automatic deploy is disabled for both web and worker. Serialization is
enforced by PostgreSQL, not by an assumption that Render starts only one
instance.

If Alembic fails, the wrapper exits non-zero and `&&` prevents Uvicorn from
starting. Application code therefore cannot start against an outdated schema.

## Deployment requirement

1. Follow `PHASE11_RELEASE_RUNBOOK.md`; its approvals, pinned commit, config
   manifest, backup verification, expected revision, and staging evidence are
   mandatory.
2. Deploy without changing `MIGRATION_MODE`; production remains `dual`.
3. Require the successful migration workflow record before the web rollout.
4. Verify `/ready`, then `/health`, before rolling out the worker.

No migration should be run manually from a developer workstation using the
production `.env`.

## Rollback

The safest application rollback is to redeploy the previous application commit
and leave additive migrations `0010`, `0011`, and `0012` in place. Older
application code does not depend on the new email columns or tables, so a schema
downgrade is not required.

Do not automatically run `alembic downgrade` during a Render rollback. A schema
downgrade can delete campaign delivery-attempt history, campaign data, and
suppression data. If a schema rollback is explicitly required, first back up
the database, keep email disabled, and have an operator review the downgrade
SQL before manually selecting the target revision.

## Operational notes

- The database role in `DATABASE_URL` must be able to connect and use PostgreSQL
  advisory locks.
- A waiting instance holds one database transaction/connection while another
  migration is running; keep the service connection budget in mind when
  scaling.
- If a future paid deployment moves the migration actor to Render's dedicated
  pre-deploy command, retain the same approval/revision gates and disable the
  GitHub migration workflow for that release to preserve one actor.
- Migration `0012` is additive, but its downgrade deletes durable delivery
  history. Prefer application rollback without schema downgrade.
