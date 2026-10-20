#!/usr/bin/env bash
# Start a throwaway Postgres with pgvector, run every test against it, then remove it.
set -euo pipefail
cd "$(dirname "$0")/../policy_api"
NAME=policy-impact-test-db
PORT=${TEST_DB_PORT:-55432}
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -e POSTGRES_PASSWORD=owner -e POSTGRES_DB=pi_test -p "127.0.0.1:$PORT:5432" pgvector/pgvector:pg16 >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 40); do docker exec "$NAME" pg_isready -U postgres -d pi_test >/dev/null 2>&1 && break; sleep 0.5; done
sleep 1
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
TEST_DATABASE_URL="postgresql://postgres:owner@127.0.0.1:$PORT/pi_test" .venv/bin/python -m pytest -q "$@"
