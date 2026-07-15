#!/usr/bin/env bash
# Shut down the Niro-managed Dify stack cleanly. Keeps data volumes (use
# reset.sh for a clean baseline) so a stop/start cycle preserves seeded state.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"
HARNESS_DIR="$REPO_ROOT/niro/harness"
PROJECT="dify-niro"
OVERRIDE_FILE="$HARNESS_DIR/docker-compose.local-images.yaml"

cd "$DOCKER_DIR"
docker compose -p "$PROJECT" -f docker-compose.yaml -f "$OVERRIDE_FILE" --env-file .env down

rm -f "$HARNESS_DIR/run/base_url"
echo "==> Dify stack stopped"
