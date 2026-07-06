#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
docker compose -f compose.yaml down --volumes --remove-orphans
docker run --rm -v "$PWD:/work" --user 0:0 alpine:3.20 sh -c 'rm -rf /work/run'
./start.sh
