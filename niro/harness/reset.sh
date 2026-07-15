#!/usr/bin/env bash
# Restore a clean baseline: wipe the app's data volumes (postgres, redis,
# weaviate, storage, plugin_daemon) and any residue a prior Niro run left,
# then bring the stack back up and re-seed. This is the "deterministic
# re-seed" fallback (no golden snapshot yet) -- it rebuilds the DB and
# credentials/fixtures.yaml together so they never desync.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"
HARNESS_DIR="$REPO_ROOT/niro/harness"
PROJECT="dify-niro"
OVERRIDE_FILE="$HARNESS_DIR/docker-compose.local-images.yaml"

echo "==> Tearing down the stack and its data volumes"
(cd "$DOCKER_DIR" && docker compose -p "$PROJECT" -f docker-compose.yaml -f "$OVERRIDE_FILE" --env-file .env down -v)

# docker-compose.yaml bind-mounts this stack's runtime data under specific
# docker/volumes/<service> subdirectories (postgres, redis, weaviate,
# plugin_daemon, app storage, sandbox/dependencies) rather than using named
# Docker volumes, so `down -v` above does not remove it -- and the services
# write those files as their own in-container uid (e.g. postgres as uid 70),
# which our host user cannot delete directly. Wipe it from inside a
# throwaway root container instead, which can. This is also what keeps a
# later `start.sh` rebuild's `docker build ... .` context free of a
# permission-denied directory it can't even stat.
#
# IMPORTANT: docker/volumes/ also holds a handful of *committed* config
# templates for services this harness doesn't run by default (sandbox/conf,
# myscale/, oceanbase/, opensearch/) -- do not blanket-`rm -rf` the whole
# tree, only the specific runtime-data subdirectories this stack populates.
echo "==> Wiping this stack's runtime data under docker/volumes"
RUNTIME_DATA_DIRS=(db mysql redis weaviate plugin_daemon app certbot "sandbox/dependencies")
EXISTING_DIRS=()
for d in "${RUNTIME_DATA_DIRS[@]}"; do
  [[ -e "$DOCKER_DIR/volumes/$d" ]] && EXISTING_DIRS+=("/niro-wipe/$d")
done
if [[ ${#EXISTING_DIRS[@]} -gt 0 ]]; then
  docker run --rm -v "$DOCKER_DIR/volumes:/niro-wipe" alpine:latest \
    sh -c 'rm -rf "$@"' _ "${EXISTING_DIRS[@]}"
fi

echo "==> Restarting"
"$HARNESS_DIR/start.sh"

echo "==> Re-seeding"
"$HARNESS_DIR/seed.sh"

echo "==> Reset complete"
