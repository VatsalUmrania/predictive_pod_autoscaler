#!/usr/bin/env bash
# scripts/deploy_operator.sh — deploy PPA operator to Minikube
# Usage: ./scripts/deploy_operator.sh [--horizon rps_t10m] [--skip-build]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HORIZON="${HORIZON:-rps_t10m}"
SKIP_BUILD="${SKIP_BUILD:-false}"
TARGET_APP="test-app"
NAMESPACE="default"
MODEL_DIR="/models/${TARGET_APP}"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --horizon)   HORIZON="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    *)           echo "Unknown arg: $1"; exit 1 ;;
  esac
done

CHAMPION_DIR="${REPO_ROOT}/model/champions/${HORIZON}"

if [[ ! -d "$CHAMPION_DIR" ]]; then
  echo "ERROR: Champion dir not found: $CHAMPION_DIR"
  echo "Run the pipeline first: python model/pipeline.py --promote-if-better ..."
  exit 1
fi

echo "=== PPA Operator Deployment ==="
echo "Horizon:    ${HORIZON}"
echo "Champions:  ${CHAMPION_DIR}"
echo ""

# ---------------------------------------------------------------------------
# 1. Build operator image inside Minikube's Docker daemon
# ---------------------------------------------------------------------------
if [[ "$SKIP_BUILD" == "false" ]]; then
  echo ">>> Building operator image inside Minikube..."
  eval "$(minikube docker-env)"
  docker build -t ppa-operator:latest -f "${REPO_ROOT}/operator/Dockerfile" "${REPO_ROOT}"
  echo "    Image built: ppa-operator:latest"
else
  echo ">>> Skipping image build (--skip-build)"
fi

# ---------------------------------------------------------------------------
# 2. Apply CRD + RBAC
# ---------------------------------------------------------------------------
echo ""
echo ">>> Applying CRD and RBAC..."
kubectl apply -f "${REPO_ROOT}/deploy/crd.yaml"
kubectl apply -f "${REPO_ROOT}/deploy/rbac.yaml"

# ---------------------------------------------------------------------------
# 3. Copy champion model artifacts to PVC via a temporary loader pod
#    (PVC is ReadWriteOnce — can't mount from two pods simultaneously)
# ---------------------------------------------------------------------------
echo ""
echo ">>> Loading champion artifacts onto PVC..."

# Ensure the PVC exists (part of operator-deployment.yaml)
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ppa-models
  namespace: ${NAMESPACE}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
EOF

# Delete any leftover loader pod
kubectl delete pod ppa-model-loader --namespace="${NAMESPACE}" --ignore-not-found --wait=true 2>/dev/null || true

# Create a temporary pod that mounts the PVC
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ppa-model-loader
  namespace: ${NAMESPACE}
spec:
  restartPolicy: Never
  containers:
  - name: loader
    image: busybox:1.36
    command: ["sleep", "300"]
    volumeMounts:
    - name: models
      mountPath: /models
  volumes:
  - name: models
    persistentVolumeClaim:
      claimName: ppa-models
EOF

echo "    Waiting for loader pod..."
kubectl wait --for=condition=Ready pod/ppa-model-loader --namespace="${NAMESPACE}" --timeout=60s

# Create target directory and copy files
kubectl exec ppa-model-loader --namespace="${NAMESPACE}" -- mkdir -p "${MODEL_DIR}"

for artifact in ppa_model.tflite scaler.pkl target_scaler.pkl; do
  src="${CHAMPION_DIR}/${artifact}"
  if [[ -f "$src" ]]; then
    kubectl cp "$src" "${NAMESPACE}/ppa-model-loader:${MODEL_DIR}/${artifact}"
    echo "    Copied ${artifact}"
  else
    echo "    WARN: ${artifact} not found in champion dir (optional)"
  fi
done

# Verify
echo "    Files on PVC:"
kubectl exec ppa-model-loader --namespace="${NAMESPACE}" -- ls -la "${MODEL_DIR}"

# Clean up loader pod
kubectl delete pod ppa-model-loader --namespace="${NAMESPACE}" --wait=true
echo "    Loader pod cleaned up"

# ---------------------------------------------------------------------------
# 4. Deploy the operator
# ---------------------------------------------------------------------------
echo ""
echo ">>> Deploying PPA operator..."
kubectl apply -f "${REPO_ROOT}/deploy/operator-deployment.yaml"
kubectl rollout status deployment/ppa-operator --namespace="${NAMESPACE}" --timeout=120s

# ---------------------------------------------------------------------------
# 5. Apply the PredictiveAutoscaler CR
# ---------------------------------------------------------------------------
echo ""
echo ">>> Applying PredictiveAutoscaler CR..."
kubectl apply -f "${REPO_ROOT}/deploy/predictiveautoscaler.yaml"

# ---------------------------------------------------------------------------
# 6. Show status
# ---------------------------------------------------------------------------
echo ""
echo "=== Deployment Complete ==="
echo ""
kubectl get ppa --namespace="${NAMESPACE}"
echo ""
echo "Watch operator logs:"
echo "  kubectl logs -l app=ppa-operator -f --namespace=${NAMESPACE}"
echo ""
echo "The operator will take ~6 minutes to warm up (12 × 30s windows)."
echo "After warmup you'll see: 'Predicted load: X req/s'"
