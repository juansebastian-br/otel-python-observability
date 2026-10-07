#!/usr/bin/env bash
# Deletes the GKE cluster and the Artifact Registry repository to stop billing.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-otel-lab}"

gcloud container clusters delete "${CLUSTER}" --region="${REGION}" --project="${PROJECT_ID}" --quiet
gcloud artifacts repositories delete otel-lab --location="${REGION}" --project="${PROJECT_ID}" --quiet
