# Azure VPS deployment

This repository owns the production Compose topology. The VPS must have Docker
Engine and Docker Compose installed before any workflow is used. Place runtime
secrets outside the repository in `/etc/qualify/backend.env` and
`/etc/qualify/frontend.env`, both mode `600`; create
`/etc/qualify/deployment.env` with only `BACKEND_IMAGE` and `FRONTEND_IMAGE`.

The current automation phase is rehearsal-only. Every automated rollout uses
`/opt/qualify/backend/deploy/docker-compose.production.yml` together with the
root-owned `/tmp/qualify-step5-compose.override.yml`. That override selects
`/etc/qualify/rehearsal-backend.env` for API/worker,
`/etc/qualify/rehearsal-frontend.env` for frontend, local Compose Redis, and
localhost-only Nginx on `127.0.0.1:8080`. Do not remove the override or activate
production networking until a separately approved cutover change.

Before configuring GitHub secrets, install the reviewed privileged helper and
minimal sudoers rule with an administrator account:

```bash
sudo install -o root -g root -m 0755 scripts/deploy-azure.sh /usr/local/sbin/qualify-deploy-azure
sudo visudo -cf deploy/qualify-deploy-azure.sudoers
sudo install -o root -g root -m 0440 deploy/qualify-deploy-azure.sudoers /etc/sudoers.d/qualify-deploy-azure
sudo visudo -cf /etc/sudoers.d/qualify-deploy-azure
```

The helper has fixed file paths, validates component-specific immutable GHCR
references, atomically changes only `BACKEND_IMAGE` or `FRONTEND_IMAGE`, and
uses protected rehearsal files without making them readable to `azureuser`.
The sudoers rule permits only this root-owned helper; it does not grant a shell
or unrestricted passwordless sudo. GitHub credentials are passed only on
standard input and use a temporary Docker configuration directory.

The API and worker share one backend image. Redis persists to the named
`qualify_redis_data` volume and is private. The API must remain a single
replica because it hosts APScheduler jobs. Migrations are never part of
container startup or deployment.

`deploy/nginx.conf` is the active HTTP bootstrap. `deploy/nginx.https.conf` is
an inactive, activation-ready TLS configuration with HTTP redirects and 443
virtual hosts for `getqualify.in`, `www.getqualify.in`, and
`api.getqualify.in`. It expects certificate files at
`/etc/letsencrypt/live/getqualify.in/{fullchain.pem,privkey.pem}` and
`/etc/letsencrypt/live/api.getqualify.in/{fullchain.pem,privkey.pem}`. Do not
mount or copy the TLS configuration over `nginx.conf` until all four files
exist and a separately approved `nginx -t` succeeds. Certificate issuance,
DNS, Azure NSG/firewall changes, and TLS activation are not performed here.

The deployment script pulls an immutable commit-SHA image, verifies the labels
on the running API and worker containers, then requires both `/health` and
`/ready` to report a registered queue worker. On any rollout failure it restores
the preceding image descriptor, container revisions, and healthy service state;
a failed rollback is itself a deployment failure.
Database rollback is deliberately excluded; use the existing approval-gated
migration workflow for any migration.

Backend and frontend deployments acquire the same exclusive
`/var/lock/qualify-production-deploy.lock` before reading deployment state and
hold it through verification or rollback. The deployment workflow starts only
after a successful `Backend CI` push run on `main` and deploys that run's exact
head SHA. A manual `workflow_dispatch` run is also available on `main`; its
optional full SHA must belong to `main`, and a blank input resolves to the
current `main` SHA. Manual runs also require a successful push-triggered
`Backend CI` run for that exact SHA. Both paths remain rehearsal-only and never
run migrations. GHCR credentials are sent over SSH standard input rather than
embedded in the remote command.

For the first Azure deployment, create the three `/etc/qualify` environment
files and validate the HTTP bootstrap and Compose configuration before starting
the workflow. With no prior running, revision-labelled API and worker pair,
automatic rollback is unavailable. If the first candidate fails, the script
stops the candidate services where possible, restores the original deployment
descriptor without recreating an unknown release, prints
`No previous Azure release is available for automatic rollback`, and fails.
Recovery is to diagnose the failed candidate, correct the deployment-only
configuration or image in a separately reviewed change, and rerun the bootstrap;
do not treat this as an application rollback or run migrations.
