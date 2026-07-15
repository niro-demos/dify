#!/usr/bin/env bash
# Niro-managed runtime: build the current checkout and start the full Dify
# service graph (postgres, redis, weaviate, sandbox, ssrf_proxy,
# plugin_daemon, agent_backend, api, worker, worker_beat, web, nginx).
#
# Serves the working tree: builds fresh `dify-api-niro:local` /
# `dify-web-niro:local` images from api/Dockerfile and web/Dockerfile every
# run (Docker layer caching keeps repeat runs fast; nothing here pulls a
# prebuilt langgenius/* app image). Supporting middleware services keep
# using docker-compose.yaml's own pinned images.
#
# Idempotent: safe to re-run against an already-running stack.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"
HARNESS_DIR="$REPO_ROOT/niro/harness"
RUN_DIR="$HARNESS_DIR/run"
PROJECT="dify-niro"
OVERRIDE_FILE="$HARNESS_DIR/docker-compose.local-images.yaml"

mkdir -p "$RUN_DIR"

echo "==> [1/4] Preparing docker/.env"
if [[ ! -f "$DOCKER_DIR/.env" ]]; then
  cp "$DOCKER_DIR/.env.example" "$DOCKER_DIR/.env"
fi
# Idempotently make sure the harness overrides block is present exactly once.
if ! grep -q "^# --- niro harness overrides" "$DOCKER_DIR/.env"; then
  cat >>"$DOCKER_DIR/.env" <<'EOF'

# --- niro harness overrides (appended by niro/harness/start.sh; docker/.env is git-ignored) ---
ALLOW_REGISTER=true
ALLOW_CREATE_WORKSPACE=true
EOF
fi

echo "==> [2/4] Building app images from the current checkout"
docker build -f "$REPO_ROOT/api/Dockerfile" -t dify-api-niro:local "$REPO_ROOT"
docker build -f "$REPO_ROOT/web/Dockerfile" -t dify-web-niro:local "$REPO_ROOT"

echo "==> [3/4] Starting the service graph"
(
  cd "$DOCKER_DIR"
  docker compose -p "$PROJECT" -f docker-compose.yaml -f "$OVERRIDE_FILE" --env-file .env up -d
)

echo "==> [4/4] Waiting for the app to become healthy"
NGINX_PORT="$(grep -E '^EXPOSE_NGINX_PORT=' "$DOCKER_DIR/.env" | tail -n1 | cut -d= -f2)"
NGINX_PORT="${NGINX_PORT:-80}"
BASE_URL="http://localhost:${NGINX_PORT}"

deadline=$((SECONDS + 420))
until curl -fsS -o /dev/null "$BASE_URL/console/api/setup" 2>/dev/null; do
  if (( SECONDS > deadline )); then
    echo "Timed out waiting for $BASE_URL to become healthy." >&2
    (cd "$DOCKER_DIR" && docker compose -p "$PROJECT" -f docker-compose.yaml -f "$OVERRIDE_FILE" ps)
    exit 1
  fi
  sleep 3
done

echo "$BASE_URL" >"$RUN_DIR/base_url"
echo "==> Dify is reachable at $BASE_URL"
