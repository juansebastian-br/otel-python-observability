#!/usr/bin/env bash
set -euo pipefail

curl -s http://localhost:8000/health
echo
curl -s http://localhost:8001/health
echo

curl -s -X POST http://localhost:8000/orders   -H 'Content-Type: application/json'   -d '{"customer_id":"customer-001","amount":125000,"currency":"COP"}'
echo

echo "Jaeger UI: http://localhost:16686"
echo "Prometheus: http://localhost:9090"
