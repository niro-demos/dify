#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

for service in api web db_postgres redis sandbox; do
  docker compose -f compose.yaml ps "$service" >/dev/null
done

python - <<'PY'
from urllib.request import urlopen

for url in ("http://127.0.0.1:5001/health", "http://127.0.0.1:3000"):
    for _ in range(120):
        try:
            with urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    break
        except Exception:
            pass
    else:
        raise SystemExit(f"healthcheck failed for {url}")
PY

docker compose -f compose.yaml exec -T redis redis-cli -a difyai123456 ping >/dev/null
docker compose -f compose.yaml exec -T db_postgres pg_isready -U postgres -d dify >/dev/null
docker compose -f compose.yaml exec -T sandbox curl -fsS http://localhost:8194/health >/dev/null
