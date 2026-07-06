#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
docker compose -f compose.yaml exec -T api sh -lc 'cd /app/api && PYTHONPATH=/app/api NIRO_DIR=/niro /app/api/.venv/bin/python /niro/harness/seed.py'
