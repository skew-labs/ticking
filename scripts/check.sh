#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python=${TICKING_TEST_PYTHON:-.venv/bin/python}
run=$(mktemp -d "${TMPDIR:-/tmp}/ticking-check.XXXXXXXX")
printf 'Isolated test evidence: %s\n' "$run"
.build/bin/core-checks
.build/bin/observer-checks
.build/bin/archive-checks "$run/archive"
.build/bin/capsule-checks "$run/capsule"
.build/bin/fault-checks "$run/fault"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout "$run/tls.key" -out "$run/tls.crt" -subj /CN=localhost -addext subjectAltName=DNS:localhost,IP:127.0.0.1 >/dev/null 2>&1
.build/bin/transport-checks "$PWD/.build/bin/metadata-recorder" "$run/transport" "$run/tls.crt" "$run/tls.key"
PYTHONPATH=engine:engine/tests "$python" -m pytest -q engine/tests
PYTHONPATH=api TICKING_DB="$run/accounts.sqlite3" TICKING_MARKETS_ENABLED=false TICKING_NEWS_ENABLED=false "$python" -m pytest -q api/server
"$python" scripts/venue-fixtures.py "$run/venue" "$PWD/.build/bin/venue-collector"
node --test packages/chart-sdk/tests/sdk.test.js
node terminal/qa/indicators-math.mjs
node terminal/qa/market-intervals-check.mjs
node terminal/qa/check-products-sdk.mjs
node terminal/qa/quality-feed-check.mjs
python3 scripts/publication-check.py
