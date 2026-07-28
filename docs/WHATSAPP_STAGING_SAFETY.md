# WhatsApp staging safety

This document applies only to the WhatsApp outreach and inbound workflow. It
does not authorize a deployment and it does not contain usable credentials.

## Required separation from production

Staging must have all of the following dedicated resources:

- a separate Postgres database and database user;
- a separate Redis instance or independently controlled Redis service;
- a separate Meta test business account, app, test number, and access token;
- a separate Airtable base while `MIGRATION_MODE=airtable` or `dual`;
- a separate backend service URL and frontend service URL;
- a staging client record with its own dashboard API-key hash;
- a separate JWT secret, webhook verify token, and webhook app secret.

Sharing any database, Redis resource, Meta phone-number ID, Airtable base, or
dashboard API key with production is a Phase 2 stop condition.

## Configuration procedure

1. Start from `backend/.env.staging.example` and
   `frontend/.env.staging.example`. Never start from a production export.
2. Keep `MIGRATION_MODE=airtable` until a later phase explicitly approves
   another staging mode.
3. Obtain staging credentials through the approved secret manager. Do not put
   them in Git, CI workflow YAML, issue text, logs, or documentation.
4. Set the frontend `BACKEND_API_URL` to the staging backend only.
5. Create the staging dashboard client/key in the staging data store only.
6. Keep optional AI credentials blank unless a later approved staging test
   explicitly requires them.
7. Keep all existing out-of-scope channel flags disabled and unchanged.

## Pre-deployment evidence and approval

Before any staging deployment, an operator who can identify both environments
must compare resource identifiers without copying secret values. Record only:

- staging and production database host/resource names are different;
- staging and production Redis host/resource names are different;
- Meta business-account, app, and phone-number IDs are different;
- Airtable base IDs are different;
- backend and frontend hostnames are different;
- staging dashboard key belongs to a staging-only client;
- no `.env`, `.env.local`, or production environment file is tracked.

Deployment approval is required after this checklist. Phase 2 itself performs no
deployment, migration, provider call, or production-secret change.

## CI safety contract

Backend tests run with `APP_ENV=test`, blank provider/data credentials, and
network/provider guards. Backend CI separately reports offline tests,
lint/type/import checks, and secret scanning.

Frontend CI rejects local/production environment files, then reports lint,
TypeScript, build, and secret-scan results. Its backend URL is loopback-only;
the build must not contact a provider or deployed backend.

Any test that requires a production credential, any outbound provider request,
or any evidence of a shared staging/production resource fails the Phase 2 stop
gate.
