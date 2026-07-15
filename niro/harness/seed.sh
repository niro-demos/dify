#!/usr/bin/env bash
# Create the deterministic baseline (tenants, accounts across every role,
# an app + dataset + API tokens per org) and (re)generate
# niro/credentials.yaml + niro/fixtures.yaml from what was actually created.
#
# Idempotent: seed_accounts.py looks up existing rows by email/name before
# creating, so re-running against an already-seeded DB just re-derives the
# same accounts/passwords and re-renders the two generated files.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"
HARNESS_DIR="$REPO_ROOT/niro/harness"
PROJECT="dify-niro"
OVERRIDE_FILE="$HARNESS_DIR/docker-compose.local-images.yaml"

compose() {
  (cd "$DOCKER_DIR" && docker compose -p "$PROJECT" -f docker-compose.yaml -f "$OVERRIDE_FILE" --env-file .env "$@")
}

echo "==> Waiting for the api container to be ready"
deadline=$((SECONDS + 180))
until compose exec -T api python3 -c "pass" >/dev/null 2>&1; do
  if (( SECONDS > deadline )); then
    echo "api container never became exec-able." >&2
    exit 1
  fi
  sleep 3
done

echo "==> Copying seed script into the api container"
# Must land inside /app/api (the app's WORKDIR): running `python3 <path>`
# only adds the script's own directory to sys.path, and app_factory /
# services / models are only importable relative to /app/api.
API_CID="$(compose ps -q api)"
docker cp "$HARNESS_DIR/seed_accounts.py" "$API_CID:/app/api/niro_seed_accounts.py"

echo "==> Running seed script"
SEED_OUTPUT="$(compose exec -T api python3 /app/api/niro_seed_accounts.py)"
echo "$SEED_OUTPUT" | sed -n '/===NIRO_SEED_JSON_START===/,/===NIRO_SEED_JSON_END===/p' \
  | sed '1d;$d' >"$HARNESS_DIR/run/seed_output.json"

if [[ ! -s "$HARNESS_DIR/run/seed_output.json" ]]; then
  echo "Seed script did not produce the expected JSON marker block. Full output:" >&2
  echo "$SEED_OUTPUT" >&2
  exit 1
fi

echo "==> Rendering niro/credentials.yaml and niro/fixtures.yaml"
python3 "$HARNESS_DIR/render_niro_files.py" <"$HARNESS_DIR/run/seed_output.json"

echo "==> Seed complete"
