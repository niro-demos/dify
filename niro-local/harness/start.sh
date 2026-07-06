#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export NIRO_COMMIT_SHA="$(git -C ../.. rev-parse HEAD 2>/dev/null || echo unknown)"
export NIRO_IMAGE_TAG="${NIRO_COMMIT_SHA}-$(git -C ../.. diff --quiet -- . ':!niro' 2>/dev/null && echo clean || echo dirty)"

if ! docker image inspect "dify-niro-api:${NIRO_IMAGE_TAG}" >/dev/null 2>&1 \
  || ! docker image inspect "dify-niro-web:${NIRO_IMAGE_TAG}" >/dev/null 2>&1; then
  if [ -d run ]; then
    echo "Refusing to build with niro/harness/run present; run reset.sh for a clean rebuild." >&2
    exit 1
  fi
  docker compose -f compose.yaml build api web
fi

mkdir -p run/logs run/app/storage run/postgres run/redis run/weaviate run/sandbox/dependencies run/sandbox/conf run/plugin_daemon
docker compose -f compose.yaml up -d

./healthcheck.sh
./seed.sh
