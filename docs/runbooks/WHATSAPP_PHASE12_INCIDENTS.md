# WhatsApp Phase 12 Incident Runbooks

These procedures preserve inbound evidence and never require customer message bodies, phone numbers, email addresses, credentials, tokens, or raw provider payloads. Record the tenant ID, correlation ID, current revision, UTC time, and operator before acting. Use operational controls to contain outbound risk; never disable webhook signature verification.

## Webhook outage

- Detection signal: webhook ACK failures/latency, missing new receipts, or Meta delivery warnings.
- Immediate containment: keep inbound endpoint and HMAC verification enabled; disable global outbound only if receipt integrity is uncertain.
- Diagnosis commands: `docker compose -f /opt/qualify/backend/deploy/docker-compose.production.yml ps`; `curl -fsS https://api.getqualify.in/health`; inspect redacted API logs by correlation ID.
- Safe recovery: restore API/Redis/database availability, then confirm new synthetic signed fixtures enqueue once. Do not replay live payloads blindly.
- Validation after recovery: `/health` and `/ready` return 200; ACK latency and receipt count recover; worker heartbeat is current.
- Escalation condition: receipts are missing, signatures fail unexpectedly, or outage exceeds 15 minutes.
- Rollback or stop condition: stop rollout and use global outbound OFF if the running revision cannot persist receipts reliably.

## Worker heartbeat loss

- Detection signal: `worker_heartbeat_missing`, zero workers, or increasing oldest queue age.
- Immediate containment: pause sequences; keep webhook/API online so receipts remain durable.
- Diagnosis commands: inspect `docker compose ... ps worker`; read redacted worker logs; query the admin metrics endpoint without printing credentials.
- Safe recovery: restart only the existing worker service after confirming Redis/database health and the intended immutable revision.
- Validation after recovery: heartbeat age falls below threshold and backlog drains without duplicate receipt/intent counts.
- Escalation condition: repeated worker exits, revision mismatch, or backlog still grows after restart.
- Rollback or stop condition: pause worker consumption if processing violates idempotency or policy invariants.

## Redis outage

- Detection signal: `redis_unavailable`, enqueue failures, or readiness failure.
- Immediate containment: keep inbound requests fail-closed and preserve database receipts; do not substitute in-process execution.
- Diagnosis commands: `docker compose ... ps redis`; inspect Redis health and API/worker redacted errors.
- Safe recovery: restore the existing private Redis service and verify queue connectivity; replay only inspected enqueue-failed receipts through the bounded API.
- Validation after recovery: Redis metric is healthy, worker heartbeat returns, and one synthetic enqueue is visible once.
- Escalation condition: Redis data loss, unexpected public exposure, or repeated connection failure.
- Rollback or stop condition: keep worker consumption paused if queue identity or durability is uncertain.

## Database outage

- Detection signal: `database_unavailable`, `/ready` failure, or receipt/control persistence errors.
- Immediate containment: global outbound OFF if available; do not bypass database-backed policy, audit, or idempotency.
- Diagnosis commands: check `/ready`, container logs, connection-pool errors, and database service status without printing the connection string.
- Safe recovery: restore database connectivity only; do not run migrations during incident recovery unless separately approved.
- Validation after recovery: revision remains `0022`, receipt/control reads work, and policy checks succeed on synthetic data.
- Escalation condition: suspected data loss, replication lag, or schema mismatch.
- Rollback or stop condition: stop sends and rollback the application if it is incompatible with the verified schema.

## Queue depth or oldest job age growth

- Detection signal: `queue_age_growing`, rising queue depth, retries, or dead letters.
- Immediate containment: pause sequences; activate worker pause only when processing is unsafe, not merely slow.
- Diagnosis commands: inspect admin queue metrics, worker heartbeat, retry counts, and redacted error types.
- Safe recovery: correct the underlying worker/provider/database issue, then drain at controlled concurrency.
- Validation after recovery: oldest age and depth fall monotonically without duplicate-send invariant breaches.
- Escalation condition: oldest age exceeds 15 minutes or growth continues for two evaluation windows.
- Rollback or stop condition: pause consumption if drain attempts create duplicates or policy blocks.

## Meta send-failure spike

- Detection signal: `meta_send_failure_spike` or provider send failures above the configured threshold.
- Immediate containment: tenant outbound OFF for isolated tenants; global outbound OFF for broad or uncertain failures.
- Diagnosis commands: inspect failure category, provider HTTP class, correlation IDs, and Meta status page; never log response payloads/tokens.
- Safe recovery: restore credentials/provider access through approved secret handling, then test only with approved synthetic recipients.
- Validation after recovery: failure rate normalizes and no unknown outcome is replayed.
- Escalation condition: permission errors, provider-wide outage, or unknown outcomes.
- Rollback or stop condition: keep outbound disabled if provider acceptance cannot be determined.

## Meta delivery/status failure spike

- Detection signal: rising `provider_status_failures_total`, status dead letters, or stale sent states.
- Immediate containment: do not resend messages solely because status is missing; preserve uncertain outcomes.
- Diagnosis commands: inspect tenant-scoped status receipts, correlation IDs, monotonic provider state, and redacted error type.
- Safe recovery: restore status processing and replay only known, tenant-owned status receipts through the bounded API.
- Validation after recovery: status receipts process once and failed/delivered/read states never regress.
- Escalation condition: status correlation cannot be linked to an outbound intent or failures grow for 15 minutes.
- Rollback or stop condition: stop replay if correlation or tenant ownership is ambiguous.

## Duplicate-send suspicion

- Detection signal: `duplicate_send_invariant_breach`, duplicate provider IDs, or operator report with matching correlation.
- Immediate containment: global outbound OFF and sequence pause; do not delete intents, receipts, or audits.
- Diagnosis commands: query intent idempotency keys, provider IDs, receipt IDs, and policy audits by tenant/correlation.
- Safe recovery: identify the first accepted send and mark uncertain duplicates for human review; never replay unknown outcomes.
- Validation after recovery: uniqueness checks return zero breaches and a synthetic retry reuses one intent.
- Escalation condition: any confirmed customer duplicate or database uniqueness violation.
- Rollback or stop condition: remain stopped until the duplicate path is reproduced offline and fixed.

## Opt-out or policy breach

- Detection signal: outbound after opt-out, unexpected policy allow, or policy-block metric anomaly.
- Immediate containment: tenant outbound OFF; use global outbound OFF if scope is unclear.
- Diagnosis commands: inspect consent, opt-out, policy decision, takeover, and intent records by tenant/correlation; avoid raw content.
- Safe recovery: correct durable policy state and validate final-send locking with offline fixtures.
- Validation after recovery: opted-out synthetic lead is blocked at preflight and final send.
- Escalation condition: any confirmed post-opt-out send or missing audit evidence.
- Rollback or stop condition: do not resume tenant outbound until compliance owner approves.

## Dead-letter growth and replay

- Detection signal: `dead_letter_present`, increasing dead-letter count, or enqueue-failed receipts.
- Immediate containment: fix the root dependency first; keep receipts and original errors unchanged.
- Diagnosis commands: use tenant-scoped `GET /api/whatsapp-operations/dead-letters?limit=50`; inspect only error type, correlation, state, and timestamps.
- Safe recovery: replay at most 10 inspected eligible receipts with trusted authentication, reason, original correlation IDs, and explicit replay limit.
- Validation after recovery: the same audit row/idempotency key is reused, original dead-letter timestamp/error remain, and final policy/control checks execute.
- Escalation condition: no tenant lead, correlation mismatch, cross-tenant request, or repeated replay failure.
- Rollback or stop condition: stop replay immediately on duplicate/unknown provider outcome or policy/control block.

## Global or tenant kill-switch activation

- Detection signal: `kill_switch_active` or an audited control transition.
- Immediate containment: confirm who activated it and why; never re-enable automatically.
- Diagnosis commands: read protected control state/audit and affected metrics using trusted authentication.
- Safe recovery: resolve the incident, then perform a version-checked audited transition with a new reason/correlation.
- Validation after recovery: inbound receipts continue throughout; only intended tenant/resource resumes.
- Escalation condition: unknown actor, unexplained activation, or control state mismatch.
- Rollback or stop condition: keep the switch OFF if evidence or authorization is incomplete.

## Backend deployment rollback

- Detection signal: health/readiness/revision mismatch, worker incompatibility, error spike, or invariant breach after deployment.
- Immediate containment: global outbound OFF when send safety is uncertain; preserve the other repository image mapping.
- Diagnosis commands: inspect `/etc/qualify/deployment.env`, container revisions, `/health`, `/ready`, worker revision, queue age, and redacted logs.
- Safe recovery: use only `/usr/local/sbin/qualify-deploy-azure` under the shared lock with the verified immutable rollback image; never run migrations from the deploy helper.
- Validation after recovery: API and worker revisions match, health/readiness are 200, frontend mapping is unchanged, and queue/Redis are healthy.
- Escalation condition: rollback image unavailable, descriptor mismatch, or schema incompatibility.
- Rollback or stop condition: stop immediately if the helper cannot preserve the other component mapping or rollback baseline.

## Offline drill execution

Run `python scripts/whatsapp_phase12_drill.py <scenario>` with one of the listed choices. The tool only prints a provider-disabled plan. Execute service tests with fake Redis/database/provider adapters; never point drill configuration at production customer endpoints.
