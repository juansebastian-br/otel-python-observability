#!/usr/bin/env bash
# Builds the images, creates (if needed) a GKE Autopilot cluster and deploys
# the full stack: services, Postgres, OTel Collector, Jaeger, Prometheus, Grafana.
#
# Usage: PROJECT_ID=my-project [REGION=us-central1] [CLUSTER=otel-lab] [IMAGE_TAG=1.0.0] scripts/deploy-gke.sh
# Re-running is safe: existing resources are reused and manifests re-applied.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-otel-lab}"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
NAMESPACE=observability
REPO=otel-lab
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

cd "$(dirname "$0")/.."

echo "==> Project ${PROJECT_ID} / region ${REGION} / cluster ${CLUSTER}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Enabling APIs"
gcloud services enable container.googleapis.com artifactregistry.googleapis.com logging.googleapis.com

echo "==> Artifact Registry"
if ! gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPO}" --repository-format=docker --location="${REGION}"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "==> Building and pushing images (linux/amd64)"
for svc in orders payments; do
  docker buildx build --platform linux/amd64 \
    -t "${REGISTRY}/${svc}-service:${IMAGE_TAG}" --push "services/${svc}"
done

echo "==> GKE Autopilot cluster"
if ! gcloud container clusters describe "${CLUSTER}" --region="${REGION}" >/dev/null 2>&1; then
  gcloud container clusters create-auto "${CLUSTER}" --region="${REGION}"
fi
gcloud container clusters get-credentials "${CLUSTER}" --region="${REGION}"

kubectl apply -f k8s/00-namespace.yaml

echo "==> Workload Identity: otel-collector KSA -> roles/logging.logWriter"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --role=roles/logging.logWriter --condition=None \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/${NAMESPACE}/sa/otel-collector" \
  >/dev/null

echo "==> Secrets (created once, never overwritten)"
if ! kubectl -n "${NAMESPACE}" get secret postgres-credentials >/dev/null 2>&1; then
  kubectl -n "${NAMESPACE}" create secret generic postgres-credentials \
    --from-literal=username=otel --from-literal=password="$(openssl rand -hex 16)"
fi
if ! kubectl -n "${NAMESPACE}" get secret grafana-credentials >/dev/null 2>&1; then
  kubectl -n "${NAMESPACE}" create secret generic grafana-credentials \
    --from-literal=username=admin --from-literal=password="$(openssl rand -hex 12)"
fi

echo "==> ConfigMaps"
apply_configmap() {
  local name=$1; shift
  kubectl -n "${NAMESPACE}" create configmap "${name}" "$@" --dry-run=client -o yaml | kubectl apply -f -
}
apply_configmap db-init --from-file=db-init.sql
apply_configmap otel-collector-config --from-file=config.yaml=otel-collector/otel-collector-gke.yaml
apply_configmap prometheus-config --from-file=prometheus/prometheus.yml
apply_configmap grafana-datasources --from-file=grafana/provisioning/datasources/
apply_configmap grafana-dashboard-provider --from-file=grafana/provisioning/dashboards/
apply_configmap grafana-dashboards --from-file=grafana/dashboards/

echo "==> Manifests"
for f in k8s/*.yaml; do
  sed -e "s|\${PROJECT_ID}|${PROJECT_ID}|g" \
      -e "s|\${REGION}|${REGION}|g" \
      -e "s|\${IMAGE_TAG}|${IMAGE_TAG}|g" "$f"
  echo "---"
done | kubectl apply -f -

# ConfigMap changes are not picked up by running pods; restart the consumers.
kubectl -n "${NAMESPACE}" rollout restart deploy/otel-collector deploy/prometheus deploy/grafana

echo "==> Waiting for rollouts"
for d in postgres otel-collector jaeger prometheus grafana payments-service orders-service; do
  kubectl -n "${NAMESPACE}" rollout status "deploy/${d}" --timeout=10m
done

kubectl -n "${NAMESPACE}" get pods
echo
echo "Grafana user: admin / password:"
echo "  kubectl -n ${NAMESPACE} get secret grafana-credentials -o jsonpath='{.data.password}' | base64 -d; echo"
echo "Next: scripts/port-forward-gke.sh  and  scripts/load.sh"
