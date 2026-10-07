#!/usr/bin/env bash
# Exposes the UIs of the GKE deployment on localhost. Ctrl+C stops all forwards.
set -euo pipefail

NAMESPACE=observability

kubectl -n "${NAMESPACE}" port-forward svc/orders-service 8000:8000 &
kubectl -n "${NAMESPACE}" port-forward svc/jaeger 16686:16686 &
kubectl -n "${NAMESPACE}" port-forward svc/prometheus 9090:9090 &
kubectl -n "${NAMESPACE}" port-forward svc/grafana 3000:3000 &

trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM

echo "Orders:     http://localhost:8000"
echo "Jaeger:     http://localhost:16686"
echo "Prometheus: http://localhost:9090"
echo "Grafana:    http://localhost:3000"
wait
