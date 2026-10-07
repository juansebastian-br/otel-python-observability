#!/usr/bin/env bash
# Sends N order requests so traces, SLIs and dashboards have data.
# Usage: [BASE_URL=http://localhost:8000] [REQUESTS=300] [CONCURRENCY=5] scripts/load.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
REQUESTS="${REQUESTS:-300}"
CONCURRENCY="${CONCURRENCY:-5}"

seq 1 "${REQUESTS}" | xargs -P "${CONCURRENCY}" -I{} \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "${BASE_URL}/orders" \
    -H 'Content-Type: application/json' \
    -d '{"customer_id":"customer-{}","amount":125000,"currency":"COP"}' \
  | sort | uniq -c | awk '{print "HTTP " $2 ": " $1 " requests"}'
