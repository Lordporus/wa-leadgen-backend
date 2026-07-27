# Render Migration Deployment Gate

Render must start the backend from the `backend/` repository root with:

```sh
python scripts/run_migrations.py && uvicorn main:app --host 0.0.0.0 --port $PORT
```

This command is defined in `render.yaml`. `DATABASE_URL` must be present in the
Render service environment.

The migration wrapper opens a PostgreSQL transaction and acquires the
application's transaction advisory lock before invoking Alembic on that exact
connection. Concurrent Render instances wait for the same lock. The lock and
all migration DDL share one transaction, which also works with a transaction
pooler. The lock is automatically released on commit, rollback, process death,
or connection loss. After the first instance upgrades the schema, each waiting
instance acquires the lock in turn, verifies that the database is already at
head, and then starts Uvicorn.

This service currently uses the free Render plan, so the migration is kept in
the start gate rather than relying on availability of a dedicated pre-deploy
command. Serialization is enforced by PostgreSQL, not by an assumption that
Render starts only one instance.

If Alembic fails, the wrapper exits non-zero and `&&` prevents Uvicorn from
starting. Application code therefore cannot start against an outdated schema.

## Deployment requirement

1. Take or confirm a current Supabase database backup.
2. Confirm the Render service has the intended `DATABASE_URL`.
3. Deploy without changing `MIGRATION_MODE`; production remains `dual`.
4. In the Render deploy log, require acquisition of the migration gate and a
   successful `alembic upgrade head` before the Uvicorn startup line.
5. Verify `/health` only after the migration command succeeds.
6. Keep `EMAIL_PLATFORM_ENABLED=false`.

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
- If a future paid deployment uses Render's dedicated pre-deploy command, run
  this same migration wrapper in that single step and remove it from
  `startCommand` to avoid duplicate gates.
- Migration `0012` is additive, but its downgrade deletes durable delivery
  history. Prefer application rollback without schema downgrade.
