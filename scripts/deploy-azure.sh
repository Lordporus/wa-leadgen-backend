#!/usr/bin/env bash
set -Eeuo pipefail

# Install this reviewed script as root-owned /usr/local/sbin/qualify-deploy-azure.
# It accepts only immutable Qualify GHCR images, uses the fixed live production
# topology, never runs migrations, and holds the shared deployment lock through
# verification or rollback.
readonly PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
readonly DEPLOY_ROOT='/opt/qualify/backend'
readonly COMPOSE_FILE="$DEPLOY_ROOT/deploy/docker-compose.production.yml"
readonly DEPLOY_ENV='/etc/qualify/deployment.env'
readonly BACKEND_ENV='/etc/qualify/backend.env'
readonly FRONTEND_ENV='/etc/qualify/frontend.env'
readonly DEPLOY_LOCK='/var/lock/qualify-production-deploy.lock'

usage() {
  echo "Usage: $0 backend <immutable-image> <commit-sha> <compose-sha256> | frontend <immutable-image> <commit-sha>" >&2
  exit 64
}

[[ "$EUID" -eq 0 ]] || { echo "qualify-deploy-azure must run as root through sudo" >&2; exit 77; }
[[ $# -ge 3 && $# -le 4 ]] || usage
component="$1"
image="$2"
commit_sha="$3"
[[ "$commit_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "commit SHA must be 40 lowercase hexadecimal characters" >&2; exit 65; }

case "$component" in
  backend)
    [[ $# -eq 4 && "$4" =~ ^[0-9a-f]{64}$ ]] || usage
    expected_compose_sha="$4"
    key='BACKEND_IMAGE'
    services=(api worker)
    expected_image="ghcr.io/lordporus/wa-leadgen-backend:$commit_sha"
    ;;
  frontend)
    [[ $# -eq 3 ]] || usage
    expected_compose_sha=''
    key='FRONTEND_IMAGE'
    services=(frontend)
    expected_image="ghcr.io/lordporus/wa-leadgen-frontend:$commit_sha"
    ;;
  *) usage ;;
esac
[[ "$image" == "$expected_image" ]] || { echo "image reference does not match the component and commit SHA" >&2; exit 65; }

for command_name in docker flock stat mktemp chown chmod mv rm readlink sha256sum; do
  command -v "$command_name" >/dev/null 2>&1 || { echo "$command_name is required" >&2; exit 69; }
done

self_path="$(readlink -f -- "$0")"
[[ "$self_path" == '/usr/local/sbin/qualify-deploy-azure' && ! -L "$0" ]] \
  || { echo "deployment helper must run from its fixed installed path" >&2; exit 77; }
[[ "$(stat -c '%U:%G:%a' "$self_path")" == 'root:root:755' ]] \
  || { echo "deployment helper ownership or mode is invalid" >&2; exit 77; }

verify_protected_file() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || return 1
  [[ "$(stat -c '%U:%G:%a' "$path")" == 'root:root:600' ]]
}

[[ -f "$COMPOSE_FILE" && ! -L "$COMPOSE_FILE" ]] || { echo "production Compose file is missing" >&2; exit 1; }
for protected_path in "$DEPLOY_ENV" "$BACKEND_ENV" "$FRONTEND_ENV"; do
  verify_protected_file "$protected_path" || { echo "protected production file failed ownership or mode validation: $protected_path" >&2; exit 1; }
done

exec 9>"$DEPLOY_LOCK" || { echo "cannot open shared deployment lock: $DEPLOY_LOCK" >&2; exit 73; }
flock -n 9 || { echo "another Qualify deployment holds $DEPLOY_LOCK" >&2; exit 75; }

if [[ -n "$expected_compose_sha" ]]; then
  actual_compose_sha="$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)"
  [[ "$actual_compose_sha" == "$expected_compose_sha" ]] \
    || { echo "live production Compose file does not match the reviewed backend commit" >&2; exit 78; }
fi

backend_image=''
frontend_image=''

validate_stored_image() {
  local image_key="$1"
  local image_value="$2"
  case "$image_key" in
    BACKEND_IMAGE)
      [[ "$image_value" =~ ^ghcr\.io/lordporus/wa-leadgen-backend:[0-9a-f]{40}$ ]]
      ;;
    FRONTEND_IMAGE)
      [[ "$image_value" =~ ^ghcr\.io/lordporus/wa-leadgen-frontend:[0-9a-f]{40}$ ]]
      ;;
    *) return 1 ;;
  esac
}

read_deployment_images() {
  local line
  backend_image=''
  frontend_image=''
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      BACKEND_IMAGE=*)
        [[ -z "$backend_image" ]] || return 1
        backend_image="${line#BACKEND_IMAGE=}"
        ;;
      FRONTEND_IMAGE=*)
        [[ -z "$frontend_image" ]] || return 1
        frontend_image="${line#FRONTEND_IMAGE=}"
        ;;
      *) return 1 ;;
    esac
  done < "$DEPLOY_ENV"
  validate_stored_image BACKEND_IMAGE "$backend_image" || return 1
  validate_stored_image FRONTEND_IMAGE "$frontend_image" || return 1
}

write_deployment_images() {
  local temp_file
  validate_stored_image BACKEND_IMAGE "$backend_image" || return 1
  validate_stored_image FRONTEND_IMAGE "$frontend_image" || return 1
  temp_file="$(mktemp '/etc/qualify/.deployment.env.XXXXXX')" || return 1
  if ! printf 'BACKEND_IMAGE=%s\nFRONTEND_IMAGE=%s\n' "$backend_image" "$frontend_image" > "$temp_file" \
    || ! chown root:root "$temp_file" \
    || ! chmod 600 "$temp_file" \
    || ! mv -f -- "$temp_file" "$DEPLOY_ENV"; then
    rm -f -- "$temp_file"
    return 1
  fi
}

set_deployment_image() {
  local image_key="$1"
  local image_value="$2"
  read_deployment_images || return 1
  case "$image_key" in
    BACKEND_IMAGE) backend_image="$image_value" ;;
    FRONTEND_IMAGE) frontend_image="$image_value" ;;
    *) return 1 ;;
  esac
  write_deployment_images
}

compose() {
  docker compose \
    --env-file "$DEPLOY_ENV" \
    -f "$COMPOSE_FILE" \
    "$@"
}

read_deployment_images || { echo "deployment.env must contain exactly one valid BACKEND_IMAGE and FRONTEND_IMAGE" >&2; exit 65; }
if [[ "$key" == 'BACKEND_IMAGE' ]]; then previous_image="$backend_image"; else previous_image="$frontend_image"; fi

docker_config="$(mktemp -d)"
[[ "$docker_config" == /tmp/tmp.* && -d "$docker_config" ]] || { echo "unsafe temporary Docker configuration path" >&2; exit 70; }
cleanup() {
  [[ "$docker_config" == /tmp/tmp.* && -d "$docker_config" ]] || return 1
  rm -rf -- "$docker_config"
}
trap cleanup EXIT
export DOCKER_CONFIG="$docker_config"

IFS= read -r ghcr_username || { echo "GHCR username was not provided on standard input" >&2; exit 66; }
IFS= read -r ghcr_token || [[ -n "${ghcr_token:-}" ]] || { echo "GHCR token was not provided on standard input" >&2; exit 66; }
[[ "$ghcr_username" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]] || { echo "GHCR username format is invalid" >&2; exit 65; }
printf '%s' "$ghcr_token" | docker login ghcr.io --username "$ghcr_username" --password-stdin >/dev/null
unset ghcr_token

single_container_id() {
  local service="$1"
  local include_stopped="${2:-false}"
  local container_id
  if [[ "$include_stopped" == 'true' ]]; then
    container_id="$(compose ps --all -q "$service")" || return 1
  else
    container_id="$(compose ps -q "$service")" || return 1
  fi
  [[ -z "$container_id" || "$container_id" != *$'\n'* ]] || return 1
  printf '%s' "$container_id"
}

container_revision() {
  docker inspect "$1" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
}

verify_service_running() {
  local container_id
  container_id="$(single_container_id "$1")" || return 1
  [[ -n "$container_id" && "$(docker inspect "$container_id" --format '{{ .State.Running }}')" == 'true' ]]
}

verify_running_revision() {
  local container_id
  container_id="$(single_container_id "$1")" || return 1
  [[ -n "$container_id" && "$(container_revision "$container_id")" == "$2" ]]
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

verify_api_compatibility() {
  for _ in $(seq 1 24); do
    if compose exec -T api python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)); assert data.get('status') == 'ready'; print(json.dumps(data, sort_keys=True))"; then
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

declare -A previous_revisions=()
previous_release_available=true
capture_previous_state() {
  local service container_id revision
  for service in "${services[@]}"; do
    container_id="$(single_container_id "$service" true)" || return 1
    if [[ -z "$container_id" || "$(docker inspect "$container_id" --format '{{ .State.Running }}')" != 'true' ]]; then
      previous_release_available=false
      continue
    fi
    revision="$(container_revision "$container_id")" || return 1
    if [[ -z "$revision" || "$revision" == '<no value>' ]]; then
      previous_release_available=false
      continue
    fi
    previous_revisions["$service"]="$revision"
  done
}

rollback_release() {
  local service
  [[ "$previous_release_available" == 'true' ]] || return 1
  set_deployment_image "$key" "$previous_image" || return 1
  compose up -d --no-deps "${services[@]}" || return 1
  for service in "${services[@]}"; do
    verify_service_running "$service" || return 1
    verify_running_revision "$service" "${previous_revisions[$service]}" || return 1
  done
  if [[ "$component" == 'backend' ]]; then verify_backend_health; else verify_frontend_health; fi
}

stop_failed_initial_candidate() {
  local service container_id
  compose stop "${services[@]}" || return 1
  set_deployment_image "$key" "$previous_image" || return 1
  for service in "${services[@]}"; do
    container_id="$(single_container_id "$service" true)" || return 1
    [[ -z "$container_id" || "$(docker inspect "$container_id" --format '{{ .State.Running }}')" == 'false' ]] || return 1
  done
}

rollback_required=false
release_verified=false
handle_deployment_error() {
  local original_status=$?
  trap - ERR
  if [[ "$rollback_required" == 'true' && "$release_verified" != 'true' ]]; then
    if [[ "$previous_release_available" == 'true' ]]; then
      rollback_release || { echo "deployment failed and rollback verification failed" >&2; exit 1; }
      echo "deployment failed; previous release was restored and verified" >&2
    else
      stop_failed_initial_candidate || { echo "No previous Azure release is available for automatic rollback; failed candidate could not be confirmed stopped" >&2; exit 1; }
      echo "No previous Azure release is available for automatic rollback" >&2
    fi
  fi
  exit "$original_status"
}

capture_previous_state
trap handle_deployment_error ERR
rollback_required=true

set_deployment_image "$key" "$image"
compose pull "${services[@]}"
if [[ "$component" == "backend" ]]; then
  # Keep the previous worker consuming while the new API proves it is ready,
  # then replace and verify the worker from the same immutable image.
  compose up -d --no-deps api
  verify_service_running api
  verify_running_revision api "$commit_sha"
  verify_api_compatibility
  compose up -d --no-deps worker
  verify_service_running worker
  verify_running_revision worker "$commit_sha"
  verify_backend_health
else
  compose up -d --no-deps frontend
  verify_service_running frontend
  verify_running_revision frontend "$commit_sha"
  verify_frontend_health
fi

release_verified=true
rollback_required=false
trap - ERR
echo "production deployment verified for commit $commit_sha"
