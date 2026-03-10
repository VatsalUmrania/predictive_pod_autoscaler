# Operator Deployment Guide

**Comprehensive guide to deploying the PPA operator from data collection to live predictions**

---

## Overview

This guide covers the complete lifecycle from collected training data → retrained model → live operator:

```
data collected         train model        convert & promote    deploy live   predictions active
      ↓                    ↓                     ↓                  ↓              ↓
  CSV ready         Keras .keras        TFLite + scalers      Pod warming up   scaling decisions
      │                 │                     │                  │              │
      └─────────────────┴─────────────────────┴──────────────────┴──────────────┘
                    scripts/ppa_redeploy.sh (one command)
```

---

## Prerequisites

✅ **Before starting, verify:**
- Kubernetes cluster running (Minikube or production)
- Prometheus deployed with 15s scrape interval
- Target deployment (`test-app` or your app) is deployed and running
- Training data collected in `data-collection/training-data/training_data_v2.csv`
- Python venv activated with `model/requirements.txt` packages installed
- Minikube Docker environment configured (`eval $(minikube docker-env)`)

```bash
# Quick verification
kubectl get nodes
kubectl get deployment test-app
kubectl get pods -n monitoring
python3 -c "import keras; print('Keras OK')"
```

---

## Quick Start (5 minutes)

### Scenario A: Deploy existing champion (no retraining)

If you already have a trained model in `model/champions/rps_t10m/`, deploy directly:

```bash
# Non-interactive deployment (doesn't ask about HPA)
./scripts/ppa_redeploy.sh --keep-hpa

# Or interactive (will ask if HPA is running)
./scripts/ppa_redeploy.sh
```

**Expected output:**
```
>>> Deploying PPA operator
>>> Applying CRD and RBAC
>>> Loading champion artifacts onto PVC
>>> Deploying PPA operator
Waiting for deployment rollout...
Pod warming up: 1/24, 2/24, ... 24/24 steps
Predicted load: 412.3 req/s → desired=5 replicas
```

**Time:** ~2 minutes

---

### Scenario B: Retrain after collecting data (10 minutes)

After HPA collected load data:

```bash
# Full pipeline: retrain + convert + deploy
./scripts/ppa_redeploy.sh --retrain --epochs 150 --delete-hpa

# Or with custom CSV path
./scripts/ppa_redeploy.sh --retrain --csv /path/to/new_data.csv

# Or skip Docker rebuild (faster iteration)
./scripts/ppa_redeploy.sh --retrain --skip-build
```

**Expected output:**
```
>>> Retraining LSTM (target=rps_t10m, lookback=24, epochs=150)
Training data: 15,600 rows
Epoch 1/150: loss=0.0234, val_loss=0.0189
...
Val MAE: 0.0156 ✓
>>> Converting Keras → TFLite
>>> Promoting to champions/rps_t10m/
>>> Building ppa-operator:latest inside Minikube
>>> Deploying operator
Predicted load: 402.1 req/s → desired=5 replicas
Scaling decisions: 8 → 5 replicas
```

**Time:** ~10 minutes (training varies by epochs)

---

## Step-by-Step Manual Deployment

If you prefer to understand each step or troubleshoot, follow this:

### Step 1: Prepare Training Data

```bash
# Training CSV should exist at:
ls -lh data-collection/training-data/training_data_v2.csv

# Verify data quality
wc -l data-collection/training-data/training_data_v2.csv  # Should be > 1000
```

**If starting fresh data collection:**
```bash
# Delete HPA first (if running)
kubectl delete hpa test-app

# Scale operator to 0 (pause PPA)
kubectl scale deployment ppa-operator --replicas=0

# Let HPA collect data ~30 minutes-2 hours of load patterns
kubectl get hpa test-app -w
```

---

### Step 2: Activate Python Environment

```bash
# Desktop/development machine
cd /run/media/vatsal/Drive/Projects/predictive_pod_autoscaler
source venv/bin/activate

# Verify packages
python3 -c "import keras, tensorflow, sklearn; print('All OK')"
```

---

### Step 3: Retrain Model

```bash
# Full retraining
python model/train.py \
  --csv data-collection/training-data/training_data_v2.csv \
  --target rps_t10m \
  --lookback 24 \
  --epochs 100 \
  --patience 20 \
  --output-dir model/artifacts

# Expected: ~5-10 min depending on CPU
# Output:
#   model/artifacts/ppa_model_rps_t10m.keras
#   model/artifacts/scaler_rps_t10m.pkl
#   model/artifacts/target_scaler_rps_t10m.pkl
#   model/artifacts/split_meta_rps_t10m.json
```

**Verify:**
```bash
ls -lh model/artifacts/ppa_model_rps_t10m.keras
```

---

### Step 4: Convert to TFLite

```bash
python model/convert.py \
  --model model/artifacts/ppa_model_rps_t10m.keras \
  --output model/artifacts/ppa_model.tflite

# Expected:
# Successfully saved TFLite model to model/artifacts/ppa_model.tflite
# Size: 278.64 KB
```

**Verify:**
```bash
file model/artifacts/ppa_model.tflite
```

---

### Step 5: Promote to Champions

```bash
mkdir -p model/champions/rps_t10m

# Copy artifacts
cp model/artifacts/ppa_model.tflite \
   model/champions/rps_t10m/ppa_model.tflite

cp model/artifacts/scaler_rps_t10m.pkl \
   model/champions/rps_t10m/scaler.pkl

cp model/artifacts/target_scaler_rps_t10m.pkl \
   model/champions/rps_t10m/target_scaler.pkl

# Verify
ls -lh model/champions/rps_t10m/
```

---

### Step 6: Scale Down Existing Operator

```bash
# If operator is already running, scale to 0
kubectl scale deployment ppa-operator --replicas=0
kubectl rollout status deployment/ppa-operator --timeout=60s

# Verify
kubectl get pods -l app=ppa-operator
```

---

### Step 7: Delete HPA (if running)

```bash
# Check if HPA exists
kubectl get hpa test-app

# Delete if present (optional, but recommended to avoid conflicts)
kubectl delete hpa test-app
```

---

### Step 8: Build Docker Image

```bash
# Must be in Minikube's Docker environment
eval $(minikube docker-env)

# Build inside Minikube (creates ppa-operator:latest)
docker build \
  -t ppa-operator:latest \
  -f operator/Dockerfile \
  . \
  --no-cache

# Verify (should be ~513MB with ai-edge-litert)
docker images ppa-operator
```

---

### Step 9: Apply CRD & RBAC

```bash
# CRD defines the PredictiveAutoscaler resource
kubectl apply -f deploy/crd.yaml

# RBAC: service account, roles, role bindings
kubectl apply -f deploy/rbac.yaml

# Verify
kubectl get crd predictiveautoscalers.ppa.example.com
kubectl get sa -l app=ppa-operator
```

---

### Step 10: Push Models to PVC

This step is **critical** — it uses the operator's own Python environment to regenerate scalers, solving pickle incompatibility when host Python ≠ pod Python.

```bash
# Create PVC if not exists
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ppa-models
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
EOF

# Copy TFLite model
kubectl cp model/champions/rps_t10m/ppa_model.tflite \
  default/ppa-model-loader:/models/test-app/

# Copy training CSV to pod
kubectl cp data-collection/training-data/training_data_v2.csv \
  default/ppa-model-loader:/tmp/training_data.csv

# Regenerate scalers inside pod (Python 3.11 environment)
kubectl exec ppa-model-loader -- python3 << 'PYEOF'
import sys, os, pickle
sys.path.insert(0, "/app")
import numpy as np
import pandas as pd
import joblib
from sklearn.preprocessing import MinMaxScaler
from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS

CSV_PATH = "/tmp/training_data.csv"
MODEL_DIR = "/models/test-app"
HORIZON = "rps_t10m"

df = pd.read_csv(CSV_PATH)
df = df.dropna(subset=FEATURE_COLUMNS + [HORIZON])

scaler = MinMaxScaler()
target_scaler = MinMaxScaler()
scaler.fit(df[FEATURE_COLUMNS].values)
target_scaler.fit(df[[HORIZON]].values)

# protocol=2 for broad Python 3.x compatibility
joblib.dump(scaler, f"{MODEL_DIR}/scaler.pkl", protocol=2)
joblib.dump(target_scaler, f"{MODEL_DIR}/target_scaler.pkl", protocol=2)
print("Scalers regenerated in pod environment")
PYEOF
```

**Verify:**
```bash
kubectl exec ppa-model-loader -- ls -lh /models/test-app/
```

---

### Step 11: Deploy Operator

```bash
# Apply Deployment (creates ppa-operator pod)
kubectl apply -f deploy/operator-deployment.yaml

# Wait for rollout
kubectl rollout status deployment/ppa-operator --timeout=120s

# Verify pod is running
kubectl get pods -l app=ppa-operator
```

---

### Step 12: Apply Custom Resource (CR)

```bash
# CR tells operator which deployment to scale & how
kubectl apply -f deploy/predictiveautoscaler.yaml

# Verify CR created
kubectl get ppa

# Watch CR status as operator warms up
kubectl get ppa test-app-ppa -w
```

---

### Step 13: Monitor Warmup

```bash
# Tail operator logs (shows warmup progress + predictions)
kubectl logs -f deployment/ppa-operator

# Expected output:
# Warming up: 1/24, 2/24, ... 24/24 steps (12 minutes)
# Then: "Predicted load: 402.3 req/s → desired=5 replicas"
```

---

## Troubleshooting Deployment

### Model fails to load: "No module named 'sklearn'"

**Cause:** `scikit-learn` missing from `operator/requirements.txt`

```bash
# Fix: Update operator/requirements.txt
echo "scikit-learn>=1.3.0" >> operator/requirements.txt

# Rebuild Docker image
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f operator/Dockerfile . --no-cache
```

---

### Scaler pickle error: "_pickle.UnpicklingError: STACK_GLOBAL requires str"

**Cause:** Scalers saved with Python 3.13 + numpy 2.x, but pod runs Python 3.11 + numpy 1.26.4

**Fix:** Regenerate scalers inside pod using step 10 above

```bash
# Verify pod's Python version
kubectl exec ppa-model-loader -- python3 --version  # Should be 3.11

# Verify numpy version
kubectl exec ppa-model-loader -- python3 -c "import numpy; print(numpy.__version__)"
```

---

### TFLite model incompatible: "FULLY_CONNECTED op v12 not supported"

**Cause:** Using older `tflite_runtime` package that doesn't support new TF ops

**Fix:** Operator uses `ai-edge-litert` which supports modern TF ops

```bash
# Verify operator/requirements.txt has:
grep "ai-edge-litert" operator/requirements.txt
```

---

### Pod stuck on "Warming up: 12/24" forever

**Cause:** `LOOKBACK_STEPS=12` in config but models trained with `lookback=24`

**Fix:** Set environment variable correctly

```bash
# Check current value
kubectl exec deployment/ppa-operator -- env | grep LOOKBACK

# Should show: PPA_LOOKBACK_STEPS=24

# If not, update operator-deployment.yaml and redeploy
```

---

### Operator's current replicas never written to CR

**Cause:** `currentReplicas` only written after stabilization (old bug)

**Fix:** Current code (main.py) writes `currentReplicas` before warmup check

```bash
# Verify CR status has currentReplicas field
kubectl get ppa test-app-ppa -o jsonpath='{.status.currentReplicas}'
```

---

### HPA and PPA fight over replicas

**Cause:** Both controllers trying to scale the same deployment

**Options:**
- Delete HPA: `kubectl delete hpa test-app` (use PPA only)
- Delete PPA: `kubectl scale deployment ppa-operator --replicas=0` (use HPA only)
- Run in parallel: not recommended (causes oscillation)

---

## Configuration After Deployment

### Change scaling parameters

```bash
# Edit CR
kubectl edit ppa test-app-ppa

# Change in spec section:
# spec:
#   minReplicas: 3         ← new minimum
#   maxReplicas: 30        ← new maximum
#   scaleUpRate: 3.0       ← faster scale-up
#   scaleDownRate: 0.3     ← slower scale-down

# Changes take effect immediately (next 30s cycle)
```

### Adjust reconciliation interval

```bash
# Change operator reconciliation timing
kubectl set env deployment/ppa-operator \
  PPA_TIMER_INTERVAL=15 \
  PPA_LOOKBACK_STEPS=8 \
  PPA_STABILIZATION_STEPS=1
```

---

## Verification Checklist

After deployment, verify each step:

- [ ] CRD registered: `kubectl get crd | grep predictive`
- [ ] RBAC configured: `kubectl get sa -l app=ppa-operator`
- [ ] Operator pod running: `kubectl get pods -l app=ppa-operator`
- [ ] Pod logs no errors: `kubectl logs deployment/ppa-operator | tail -20`
- [ ] Model files in PVC: `kubectl exec ppa-model-loader -- ls /models/test-app/`
- [ ] CR created: `kubectl get ppa`
- [ ] CR warmed up: `kubectl get ppa test-app-ppa`
- [ ] Predictions active: `kubectl logs deployment/ppa-operator | grep "Predicted"`
- [ ] Scaling decisions: `kubectl logs deployment/ppa-operator | grep "Scaling"` or `kubectl logs deployment/ppa-operator | grep "Patched"`

---

## Automated Deployment (Recommended)

For most deployments, use the automated script:

```bash
# Deploy existing champion (fastest)
./scripts/ppa_redeploy.sh

# Retrain + deploy
./scripts/ppa_redeploy.sh --retrain

# Full control
./scripts/ppa_redeploy.sh --retrain --epochs 150 --delete-hpa --skip-build

# Help
./scripts/ppa_redeploy.sh --help
```

See [scripts/ppa_redeploy.sh](../../scripts/ppa_redeploy.sh) for source.

---

## Production Considerations

### Resource Limits
The operator pod requires:
- **CPU:** 500m (typical: 100-200m)
- **Memory:** 512Mi (typical: 200-300Mi)

Monitor in production:
```bash
kubectl top pods -l app=ppa-operator
```

### Model Updates
To deploy a new model without restarting:
```bash
# Copy new model to PVC
kubectl cp model/champions/rps_t10m/ppa_model.tflite \
  default/ppa-model-loader:/models/test-app/

# Operator reloads on next cycle (~30s)
kubectl logs -f deployment/ppa-operator
```

### High Availability (HA)
For production, consider:
- PVC with replicated storage (e.g., Ceph RBD, AWS EBS)
- Multiple operator replicas with leader election (requires etcd coordination)
- Backup of champion models: `git add model/champions/` → automatic backup

---

## See Also

- [Configuration Reference](./configuration.md) — Environment variables and CR tuning
- [Architecture](./architecture.md) — Detailed internals and reconciliation flow
- [Commands](./commands.md) — Useful kubectl commands for operations
- [Troubleshooting](./troubleshooting.md) — Common issues and solutions
- [ML Pipeline Guide](../architecture/ml_pipeline.md) — Training and model evaluation
