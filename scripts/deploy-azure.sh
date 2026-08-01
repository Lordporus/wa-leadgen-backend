#!/usr/bin/env bash
set -Eeuo pipefail

# Runs only on the VPS after GitHub Actions has pushed an immutable image.
# It never runs migrations. Any failure after rollout preparation invokes a
# mandatory, verified restoration of the preceding deployment descriptor.
readonly DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/qualify/wa-leadgen-backend}"
readonly COMPOSE_FILE="$DEPLOY_ROOT/deploy/docker-compose.production.yml"
readonly DEPLOY_ENV="${DEPLOY_ENV:-/etc/qualify/deployment.env}"
readonly DEPLOY_LOCK="${DEPLOY_LOCK:-/var/lock/qualify-production-deploy.lock}"

usage() {
  echo "Usage: $0 backend <image> <commit-sha> | frontend <image> <commit-sha>" >&2
  exit 64
}

[[ $# -eq 3 ]] || usage
component="$1"
image="$2"
commit_sha="$3"
case "$component" in backend|frontend) ;; *) usage ;; esac

command -v flock >/dev/null 2>&1 || { echo "flock is required for production deployment" >&2; exit 69; }
if ! { exec 9>"$DEPLOY_LOCK"; }; then
  echo "cannot open shared deployment lock: $DEPLOY_LOCK" >&2
  exit 73
fi
if ! flock -n 9; then
  echo "another Qualify production deployment holds $DEPLOY_LOCK" >&2
  exit 75
fi

[[ -f "$COMPOSE_FILE" && -f "$DEPLOY_ENV" ]] || { echo "deployment prerequisites are missing" >&2; exit 1; }
[[ -f /etc/qualify/backend.env && -f /etc/qualify/frontend.env ]] || { echo "runtime environment files are missing" >&2; exit 1; }

compose() { docker compose --env-file "$DEPLOY_ENV" -f "$COMPOSE_FILE" "$@"; }
key="BACKEND_IMAGE"; services=(api worker)
if [[ "$component" == "frontend" ]]; then key="FRONTEND_IMAGE"; services=(frontend); fi

backup="$(mktemp)"
candidate="$(mktemp)"
cleanup() { rm -f "$backup" "$candidate"; }
trap cleanup EXIT
cp "$DEPLOY_ENV" "$backup"
grep -v "^${key}=" "$backup" > "$candidate" || true
printf '%s=%s\n' "$key" "$image" >> "$candidate"

declare -A previous_container_ids=()
declare -A previous_revisions=()
previous_release_available=true

single_container_id() {
  local service="$1"
  local include_stopped="${2:-false}"
  local container_id
  if [[ "$include_stopped" == "true" ]]; then
    container_id="$(compose ps --all -q "$service")" || return 1
  else
    container_id="$(compose ps -q "$service")" || return 1
  fi
  [[ -z "$container_id" || "$container_id" != *$'\n'* ]] || return 1
  printf '%s' "$container_id"
}

container_revision() {
  local container_id="$1"
  docker inspect "$container_id" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
}

verify_service_running() {
  local service="$1"
  local container_id state
  container_id="$(single_container_id "$service")" || return 1
  [[ -n "$container_id" ]] || return 1
  state="$(docker inspect "$container_id" --format '{{ .State.Running }}')" || return 1
  [[ "$state" == "true" ]]
}

verify_running_revision() {
  local service="$1"
  local expected_revision="$2"
  local container_id actual_revision
  container_id="$(single_container_id "$service")" || return 1
  [[ -n "$container_id" ]] || return 1
  actual_revision="$(container_revision "$container_id")" || return 1
  [[ "$actual_revision" == "$expected_revision" ]]
}

verify_backend_health() {
  for _ in $(seq 1 24); do
    if compose exec -T api python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)); queue=data.get('whatsapp_queue', {}); assert data.get('status') == 'ok' and queue.get('ready') is True and int(queue.get('workers', 0)) >= 1" \
      && compose exec -T api python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)); queue=data.get('whatsapp_queue', {}); assert data.get('status') == 'ready' and queue.get('ready') is True and int(queue.get('workers', 0)) >= 1"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

verify_frontend_health() {
  for _ in $(seq 1 24); do
    if compose exec -T frontend node -e "fetch('http://127.0.0.1:3000').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

capture_previous_state() {
  local service container_id state revision
  for service in "${services[@]}"; do
    container_id="$(single_container_id "$service" true)" || return 1
    if [[ -z "$container_id" ]]; then
      previous_release_available=false
      continue
    fi
    previous_container_ids["$service"]="$container_id"
    state="$(docker inspect "$container_id" --format '{{ .State.Running }}')" || return 1
    if [[ "$state" != "true" ]]; then
      previous_release_available=false
      continue
    fi
    revision="$(container_revision "$container_id")" || return 1
    if [[ -z "$revision" || "$revision" == "<no value>" ]]; then
      previous_release_available=false
      continue
    fi
    previous_revisions["$service"]="$revision"
  done
}

rollback_release() {
  local service previous_revision
  [[ "$previous_release_available" == "true" ]] || return 1
  if ! install -m 600 "$backup" "$DEPLOY_ENV"; then return 1; fi
  if ! compose up -d --no-deps "${services[@]}"; then return 1; fi
  for service in "${services[@]}"; do
    if ! verify_service_running "$service"; then return 1; fi
    previous_revision="${previous_revisions[$service]}"
    if ! verify_running_revision "$service" "$previous_revision"; then
      return 1
    fi
  done
  if [[ "$component" == "backend" ]]; then
    verify_backend_health
  else
    verify_frontend_health
  fi
}

stop_failed_initial_candidate() {
  local service container_id state
  if ! compose stop "${services[@]}"; then return 1; fi
  if ! install -m 600 "$backup" "$DEPLOY_ENV"; then return 1; fi
  for service in "${services[@]}"; do
    container_id="$(single_container_id "$service" true)" || return 1
    [[ -n "$container_id" ]] || continue
    state="$(docker inspect "$container_id" --format '{{ .State.Running }}')" || return 1
    [[ "$state" == "false" ]] || return 1
  done
}

rollback_required=false
release_verified=false

handle_deployment_error() {
  local original_status=$?
  trap - ERR
  if [[ "$rollback_required" == "true" && "$release_verified" != "true" ]]; then
    if [[ "$previous_release_available" == "true" ]]; then
      if rollback_release; then
        echo "deployment failed; previous release was restored and verified" >&2
      else
        echo "deployment failed and rollback verification failed" >&2
        exit 1
      fi
    else
      if ! stop_failed_initial_candidate; then
        echo "No previous Azure release is available for automatic rollback; failed candidate could not be confirmed stopped" >&2
        exit 1
      fi
      echo "No previous Azure release is available for automatic rollback" >&2
    fi
  fi
  exit "$original_status"
}

capture_previous_state
trap handle_deployment_error ERR
rollback_required=true

install -m 600 "$candidate" "$DEPLOY_ENV"
compose pull "${services[@]}"
compose up -d --no-deps "${services[@]}"

for service in "${services[@]}"; do
  verify_service_running "$service"
  verify_running_revision "$service" "$commit_sha"
done

if [[ "$component" == "backend" ]]; then
  verify_backend_health
else
  verify_frontend_health
fi

release_verified=true
rollback_required=false
trap - ERR
echo "deployment verified for commit $commit_sha"
