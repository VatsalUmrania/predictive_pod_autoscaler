#!/usr/bin/env bash
set -e

# onboard_app.sh — Generates and applies PPA CRs for a new application.
# It creates three PredictiveAutoscaler resources for 3m, 5m, and 10m horizons.
# 3m & 5m act as observers, while 10m actively scales.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_FILE="${REPO_ROOT}/deploy/templates/predictiveautoscaler.yaml.tpl"

# Defaults
APP_NAME=""
TARGET_DEPLOYMENT=""
NAMESPACE="default"
MIN_REPLICAS=1
MAX_REPLICAS=10
RPS_CAPACITY=20
SAFETY_FACTOR=1.15
SCALE_UP_RATE=2.0
SCALE_DOWN_RATE=1.0

function usage() {
  echo "Usage: $0 --app-name <name> --target <deployment> [options]"
  echo "Options:"
  echo "  --app-name         Logical application name (used for /models/{app})"
  echo "  --target           Target Kubernetes Deployment name"
  echo "  --namespace        Target namespace (default: default)"
  echo "  --min-replicas     Minimum replicas (default: 1)"
  echo "  --max-replicas     Maximum replicas (default: 10)"
  echo "  --rps-capacity     RPS threshold per pod (default: 20)"
  echo "  --safety-factor    Safety multiplier buffer (default: 1.15)"
  echo "  --scale-up         Max scale-up rate (default: 2.0)"
  echo "  --scale-down       Max scale-down rate (default: 1.0)"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-name) APP_NAME="$2"; shift 2 ;;
    --target) TARGET_DEPLOYMENT="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --min-replicas) MIN_REPLICAS="$2"; shift 2 ;;
    --max-replicas) MAX_REPLICAS="$2"; shift 2 ;;
    --rps-capacity) RPS_CAPACITY="$2"; shift 2 ;;
    --safety-factor) SAFETY_FACTOR="$2"; shift 2 ;;
    --scale-up) SCALE_UP_RATE="$2"; shift 2 ;;
    --scale-down) SCALE_DOWN_RATE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1"; usage ;;
  esac
done

if [[ -z "$APP_NAME" || -z "$TARGET_DEPLOYMENT" ]]; then
  echo "Error: --app-name and --target are required."
  usage
fi

export APP_NAME
export TARGET_DEPLOYMENT
export NAMESPACE
export MIN_REPLICAS
export MAX_REPLICAS
export RPS_CAPACITY
export SAFETY_FACTOR
export SCALE_UP_RATE
export SCALE_DOWN_RATE

OUTPUT_DIR="${REPO_ROOT}/deploy/generated-manifests/${APP_NAME}"
mkdir -p "${OUTPUT_DIR}"

echo "Onboarding ${APP_NAME} -> Deployment ${TARGET_DEPLOYMENT} in ns ${NAMESPACE}"

# 1. 3-Minute Observer
export HORIZON="rps_t3m"
export HORIZON_CLEAN="${HORIZON//_/-}"
export OBSERVER_MODE="true"
envsubst < "${TEMPLATE_FILE}" > "${OUTPUT_DIR}/ppa-${HORIZON}.yaml"
echo "- Generated ${OUTPUT_DIR}/ppa-${HORIZON}.yaml (Observer)"

# 2. 5-Minute Observer
export HORIZON="rps_t5m"
export HORIZON_CLEAN="${HORIZON//_/-}"
export OBSERVER_MODE="true"
envsubst < "${TEMPLATE_FILE}" > "${OUTPUT_DIR}/ppa-${HORIZON}.yaml"
echo "- Generated ${OUTPUT_DIR}/ppa-${HORIZON}.yaml (Observer)"

# 3. 10-Minute Active Scaler
export HORIZON="rps_t10m"
export HORIZON_CLEAN="${HORIZON//_/-}"
export OBSERVER_MODE="false"
envsubst < "${TEMPLATE_FILE}" > "${OUTPUT_DIR}/ppa-${HORIZON}.yaml"
echo "- Generated ${OUTPUT_DIR}/ppa-${HORIZON}.yaml (Active)"

echo ""
echo "Applying manifests..."
kubectl apply -f "${OUTPUT_DIR}/ppa-rps_t3m.yaml"
kubectl apply -f "${OUTPUT_DIR}/ppa-rps_t5m.yaml"
kubectl apply -f "${OUTPUT_DIR}/ppa-rps_t10m.yaml"

echo ""
echo "Successfully onboarded application '${APP_NAME}'."
echo "Models should be uploaded to /models/${APP_NAME} by running ppa_redeploy.sh."
