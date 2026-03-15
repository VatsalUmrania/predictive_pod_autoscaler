#!/usr/bin/env bash
# scripts/ppa_redeploy.sh
# ─────────────────────────────────────────────────────────────────────────────
# One-shot script to go from "data collected" → live PPA predictions.
#
# Full flow (all steps can be skipped individually):
#   1. Retrain LSTM  (--retrain)
#   2. Convert Keras → TFLite  (auto when --retrain)
#   3. Promote artifacts to champions dir
#   4. Delete HPA if running  (asks unless --delete-hpa / --keep-hpa)
#   5. Scale down old operator
#   6. Build Docker image inside Minikube  (skip with --skip-build)
#   7. Push model artifacts to PVC  (uses ppa-operator image to fix scaler
#      pickle compatibility between host Python and pod Python)
#   8. Apply Deployment + CR
#   9. Tail logs  (skip with --no-watch)
#
# Usage examples:
#   ./scripts/ppa_redeploy.sh                         # deploy existing champion, ask about HPA
#   ./scripts/ppa_redeploy.sh --retrain               # retrain then deploy
#   ./scripts/ppa_redeploy.sh --retrain --epochs 150  # retrain with more epochs
#   ./scripts/ppa_redeploy.sh --skip-build --no-watch # fast redeploy, no rebuild
#   ./scripts/ppa_redeploy.sh --delete-hpa            # non-interactive HPA deletion
#
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
step()  { echo -e "\n${CYAN}${BOLD}>>> $*${NC}"; }
ok()    { echo -e "${GREEN}    ✓ $*${NC}"; }
warn()  { echo -e "${YELLOW}    ⚠ $*${NC}"; }
die()   { echo -e "${RED}ERROR: $*${NC}" >&2; exit 1; }

# ── Defaults ──────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HORIZON="rps_t10m"
CSV_PATH="${REPO_ROOT}/data-collection/training-data/training_data_v2.csv"
LOOKBACK=24
EPOCHS=100
PATIENCE=20
TARGET_APP="test-app"
NAMESPACE="default"
MODEL_DIR="/models/${TARGET_APP}"

DO_RETRAIN=false
SKIP_BUILD=false
DELETE_HPA=""        # empty = ask interactively
WATCH_LOGS=true
VENV_PATH="${REPO_ROOT}/venv"

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-name)       TARGET_APP="$2"; shift 2 ;;
    --retrain)        DO_RETRAIN=true; shift ;;
    --horizon)        HORIZON="$2"; shift 2 ;;
    --csv)            CSV_PATH="$2"; shift 2 ;;
    --epochs)         EPOCHS="$2"; shift 2 ;;
    --lookback)       LOOKBACK="$2"; shift 2 ;;
    --patience)       PATIENCE="$2"; shift 2 ;;
    --skip-build)     SKIP_BUILD=true; shift ;;
    --delete-hpa)     DELETE_HPA=yes; shift ;;
    --keep-hpa)       DELETE_HPA=no; shift ;;
    --no-watch)       WATCH_LOGS=false; shift ;;
    --venv)           VENV_PATH="$2"; shift 2 ;;
    -h|--help)
      sed -n '/^# Usage/,/^# ─/p' "$0" | head -20
      exit 0 ;;
    *) die "Unknown argument: $1  (use --help)" ;;
  esac
done

ARTIFACTS_DIR="${REPO_ROOT}/model/artifacts"
CHAMPION_DIR="${REPO_ROOT}/model/champions/${TARGET_APP}/${HORIZON}"

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}╔══════════════════════════════════════════════════╗"
echo -e "║      PPA Redeploy — $(date '+%Y-%m-%d %H:%M')           ║"
echo -e "╚══════════════════════════════════════════════════╝${NC}"
echo "  App      : ${TARGET_APP}"
echo "  Horizon  : ${HORIZON}"
echo "  Retrain  : ${DO_RETRAIN}"
echo "  Skip bld : ${SKIP_BUILD}"
echo "  CSV      : ${CSV_PATH}"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Retrain + convert
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$DO_RETRAIN" == "true" ]]; then
  step "Retraining LSTM for ${TARGET_APP} (target=${HORIZON}, lookback=${LOOKBACK}, epochs=${EPOCHS})"

  [[ -f "$CSV_PATH" ]] || die "Training CSV not found: $CSV_PATH"
  echo "    Rows in CSV: $(wc -l < "$CSV_PATH")"

  # Activate venv if present
  if [[ -f "${VENV_PATH}/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "${VENV_PATH}/bin/activate"
    ok "Activated venv at ${VENV_PATH}"
  else
    warn "No venv found at ${VENV_PATH} — using system Python"
  fi

  # Map horizon label → target column name
  # e.g. rps_t10m is already the column name
  TARGET_COL="${HORIZON}"

  python "${REPO_ROOT}/model/train.py" \
    --csv       "${CSV_PATH}" \
    --lookback  "${LOOKBACK}" \
    --epochs    "${EPOCHS}" \
    --patience  "${PATIENCE}" \
    --target    "${TARGET_COL}" \
    --output-dir "${ARTIFACTS_DIR}"

  ok "Training done"

  # ── Convert Keras → TFLite ──────────────────────────────────────────────
  step "Converting Keras model → TFLite"

  KERAS_MODEL="${ARTIFACTS_DIR}/ppa_model_${TARGET_COL}.keras"
  [[ -f "$KERAS_MODEL" ]] || die "Keras model not found after training: $KERAS_MODEL"

  TFLITE_OUT="${ARTIFACTS_DIR}/ppa_model.tflite"
  python "${REPO_ROOT}/model/convert.py" \
    --model  "${KERAS_MODEL}" \
    --output "${TFLITE_OUT}"

  ok "Converted → ${TFLITE_OUT}"

  # ── Promote to champions ────────────────────────────────────────────────
  step "Promoting artifacts to ${CHAMPION_DIR}"
  mkdir -p "${CHAMPION_DIR}"

  cp "${TFLITE_OUT}" "${CHAMPION_DIR}/ppa_model.tflite"
  cp "${ARTIFACTS_DIR}/scaler_${TARGET_COL}.pkl"        "${CHAMPION_DIR}/scaler.pkl"
  cp "${ARTIFACTS_DIR}/target_scaler_${TARGET_COL}.pkl" "${CHAMPION_DIR}/target_scaler.pkl"
  cp "${ARTIFACTS_DIR}/split_meta_${TARGET_COL}.json"   "${CHAMPION_DIR}/split_meta_${HORIZON}.json" 2>/dev/null || true

  ok "Promoted model + scalers to champions"
fi

# ── Verify champion dir exists ────────────────────────────────────────────────
[[ -d "$CHAMPION_DIR" ]] || die "Champion dir not found: $CHAMPION_DIR\nRun with --retrain or run model/train.py manually first."
[[ -f "${CHAMPION_DIR}/ppa_model.tflite" ]] || die "ppa_model.tflite missing from ${CHAMPION_DIR}"

echo ""
echo "  Champion artifacts:"
ls -lh "${CHAMPION_DIR}"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Handle HPA
# ─────────────────────────────────────────────────────────────────────────────
step "Checking HPA"
if kubectl get hpa "${TARGET_APP}" --namespace="${NAMESPACE}" &>/dev/null; then
  HPA_STATUS=$(kubectl get hpa "${TARGET_APP}" --namespace="${NAMESPACE}" \
    -o jsonpath='{.status.currentReplicas}/{.spec.maxReplicas} replicas, CPU={.status.currentMetrics[0].resource.current.averageUtilization}%' 2>/dev/null || echo "running")
  warn "HPA '${TARGET_APP}' is active — ${HPA_STATUS}"
  warn "Leaving HPA running alongside PPA will cause scaling conflicts!"

  if [[ -z "$DELETE_HPA" ]]; then
    echo -e "\n    Delete HPA now? [y/N] \c"
    read -r answer
    [[ "$answer" =~ ^[Yy]$ ]] && DELETE_HPA=yes || DELETE_HPA=no
  fi

  if [[ "$DELETE_HPA" == "yes" ]]; then
    kubectl delete hpa "${TARGET_APP}" --namespace="${NAMESPACE}"
    ok "HPA deleted"
  else
    warn "Keeping HPA — PPA and HPA will both run (may conflict)"
  fi
else
  ok "No HPA found for '${TARGET_APP}'"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Scale down existing operator
# ─────────────────────────────────────────────────────────────────────────────
step "Scaling down existing operator (if any)"
if kubectl get deployment ppa-operator --namespace="${NAMESPACE}" &>/dev/null; then
  kubectl scale deployment ppa-operator --replicas=0 --namespace="${NAMESPACE}"
  kubectl rollout status deployment/ppa-operator --namespace="${NAMESPACE}" --timeout=60s || true
  ok "Operator scaled to 0"
else
  ok "No existing operator deployment found"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Build Docker image inside Minikube's Docker daemon
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "false" ]]; then
  step "Building ppa-operator:latest inside Minikube"
  eval "$(minikube docker-env)"
  docker build \
    -t ppa-operator:latest \
    -f "${REPO_ROOT}/operator/Dockerfile" \
    "${REPO_ROOT}"
  ok "Image built: ppa-operator:latest"
else
  step "Skipping image build (--skip-build)"
  eval "$(minikube docker-env)"   # still need Minikube env for kubectl
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Apply CRD + RBAC
# ─────────────────────────────────────────────────────────────────────────────
step "Applying CRD and RBAC"
kubectl apply -f "${REPO_ROOT}/deploy/crd.yaml"
kubectl apply -f "${REPO_ROOT}/deploy/rbac.yaml"
ok "CRD + RBAC applied"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Push model artifacts to PVC
#
# We use the ppa-operator image (Python 3.11 + numpy 1.26.4) as the loader pod
# so that we can regenerate the scalers with the pod's Python. This is required
# when the host trained with Python 3.13/numpy 2.x — pickle format differences
# cause "STACK_GLOBAL requires str" errors if we copy raw .pkl files directly.
# ─────────────────────────────────────────────────────────────────────────────
step "Pushing model artifacts to PVC"

# Ensure PVC exists
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

# Remove any stale loader pod
kubectl delete pod ppa-model-loader --namespace="${NAMESPACE}" \
  --ignore-not-found --wait=true 2>/dev/null || true

# Spin up a loader pod using the operator image
# (has Python 3.11 + sklearn + numpy 1.26.4 — same as inference runtime)
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
    image: ppa-operator:latest
    imagePullPolicy: Never
    command: ["sleep", "300"]
    volumeMounts:
    - name: models
      mountPath: /models
  volumes:
  - name: models
    persistentVolumeClaim:
      claimName: ppa-models
EOF

echo "    Waiting for loader pod to be Ready..."
kubectl wait --for=condition=Ready pod/ppa-model-loader \
  --namespace="${NAMESPACE}" --timeout=90s

# Copy training CSV to pod once (shared by all 3 horizon uploads)
kubectl cp "${CSV_PATH}" \
  "${NAMESPACE}/ppa-model-loader:/tmp/training_data.csv"
ok "Copied training CSV to pod"

# Inline Python scaler-regen script (runs inside pod per horizon)
REGEN_SCRIPT=$(cat <<'PYEOF'
import sys, os
sys.path.insert(0, "/app")
import pandas as pd
import joblib
from sklearn.preprocessing import MinMaxScaler
from common.feature_spec import FEATURE_COLUMNS

CSV_PATH   = "/tmp/training_data.csv"
MODEL_DIR  = sys.argv[1]
HORIZON    = sys.argv[2]

print(f"[{HORIZON}] Loading CSV ...")
df = pd.read_csv(CSV_PATH)
feature_cols = FEATURE_COLUMNS
target_col   = HORIZON

missing = [c for c in feature_cols if c not in df.columns]
if missing:
    print(f"WARNING: missing columns: {missing}")
    feature_cols = [c for c in feature_cols if c in df.columns]

if target_col not in df.columns:
    print(f"ERROR: target '{target_col}' not in CSV. Cols: {list(df.columns)}")
    sys.exit(1)

df = df.dropna(subset=feature_cols + [target_col])
scaler        = MinMaxScaler()
target_scaler = MinMaxScaler()
scaler.fit(df[feature_cols].values)
target_scaler.fit(df[[target_col]].values)

joblib.dump(scaler,        os.path.join(MODEL_DIR, "scaler.pkl"),        protocol=2)
joblib.dump(target_scaler, os.path.join(MODEL_DIR, "target_scaler.pkl"), protocol=2)
print(f"[{HORIZON}] Scaler regen complete -> {MODEL_DIR}")
PYEOF
)

# Upload all 3 champion horizons — each to /models/<app_name>/<horizon>/
for UPLOAD_HORIZON in rps_t3m rps_t5m rps_t10m; do
  UPLOAD_MODEL_DIR="/models/${TARGET_APP}/${UPLOAD_HORIZON}"
  UPLOAD_CHAMPION_DIR="${REPO_ROOT}/model/champions/${TARGET_APP}/${UPLOAD_HORIZON}"

  if [[ ! -f "${UPLOAD_CHAMPION_DIR}/ppa_model.tflite" ]]; then
    warn "Skipping ${UPLOAD_HORIZON}: no ppa_model.tflite in ${UPLOAD_CHAMPION_DIR}"
    continue
  fi

  echo ""
  echo "    ── Uploading ${TARGET_APP}/${UPLOAD_HORIZON} → ${UPLOAD_MODEL_DIR} ──"

  kubectl exec ppa-model-loader --namespace="${NAMESPACE}" \
    -- mkdir -p "${UPLOAD_MODEL_DIR}"

  kubectl cp "${UPLOAD_CHAMPION_DIR}/ppa_model.tflite" \
    "${NAMESPACE}/ppa-model-loader:${UPLOAD_MODEL_DIR}/ppa_model.tflite"
  ok "Copied ppa_model.tflite (${UPLOAD_HORIZON})"

  kubectl exec ppa-model-loader --namespace="${NAMESPACE}" \
    -- python3 -c "${REGEN_SCRIPT}" "${UPLOAD_MODEL_DIR}" "${UPLOAD_HORIZON}"
  ok "Scalers regenerated (${UPLOAD_HORIZON})"

  kubectl cp "${NAMESPACE}/ppa-model-loader:${UPLOAD_MODEL_DIR}/scaler.pkl" \
    "${UPLOAD_CHAMPION_DIR}/scaler.pkl"
  kubectl cp "${NAMESPACE}/ppa-model-loader:${UPLOAD_MODEL_DIR}/target_scaler.pkl" \
    "${UPLOAD_CHAMPION_DIR}/target_scaler.pkl"
  ok "Scalers synced back to host: ${UPLOAD_CHAMPION_DIR}"
done

# Show final PVC tree
echo ""
echo "    All models present in PVC for ${TARGET_APP}:"
kubectl exec ppa-model-loader --namespace="${NAMESPACE}" \
  -- ls -lah /models/${TARGET_APP} 2>/dev/null || true
kubectl exec ppa-model-loader --namespace="${NAMESPACE}" \
  -- find /models/${TARGET_APP} -name '*.tflite' -o -name '*.pkl' | sort || true

# Clean up loader pod
kubectl delete pod ppa-model-loader --namespace="${NAMESPACE}" --wait=true
ok "Loader pod removed"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Deploy operator
# ─────────────────────────────────────────────────────────────────────────────
step "Deploying PPA operator"
kubectl apply -f "${REPO_ROOT}/deploy/operator-deployment.yaml"
kubectl rollout status deployment/ppa-operator \
  --namespace="${NAMESPACE}" --timeout=120s
ok "Operator deployment rolled out"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Apply PredictiveAutoscaler CR
# ─────────────────────────────────────────────────────────────────────────────
step "Applying PredictiveAutoscaler CR"
kubectl apply -f "${REPO_ROOT}/deploy/predictiveautoscaler.yaml"
ok "CR applied"

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗"
echo -e "║             Deployment Complete ✓                ║"
echo -e "╚══════════════════════════════════════════════════╝${NC}"
echo ""
kubectl get ppa --namespace="${NAMESPACE}" 2>/dev/null || true
echo ""
echo "  Operator pod:"
kubectl get pod -l app=ppa-operator --namespace="${NAMESPACE}"
echo ""
WARMUP_MIN=$(( LOOKBACK / 2 ))   # 24 steps × 30s = 12 min
echo -e "  ${YELLOW}Warmup: ~${WARMUP_MIN} minutes (${LOOKBACK} × 30s steps)${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 — Tail logs
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$WATCH_LOGS" == "true" ]]; then
  echo -e "  ${CYAN}Tailing operator logs — Ctrl+C to exit${NC}"
  echo "  (Grep: 'Predicted|Scaling|Warming|ERROR')"
  echo ""
  sleep 3
  kubectl logs -l app=ppa-operator \
    --namespace="${NAMESPACE}" \
    -f --tail=50 \
    | grep --line-buffered -E 'Predicted|Scaling|Patched|Warming|ERROR|WARN|champion|model' \
    || true
fi
