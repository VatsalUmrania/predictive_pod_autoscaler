# PPA Model Upload Fixes - Testing Guide

## Overview
This guide helps you verify that the model upload and loading fixes are working correctly.

---

## Phase 1: Pre-Deployment Diagnostics

### Check TFLite Runtime Availability Locally
```bash
# Test if tflite-runtime is installed in your environment
python3 -c "import tflite_runtime.interpreter; print('✓ tflite-runtime available')"

# Test if ai_edge_litert is available
python3 -c "import ai_edge_litert; print('✓ ai_edge_litert available')"

# Run full diagnostics (requires the updated code)
python3 -c "
from src.ppa.operator.diagnostics import check_tflite_runtime, get_platform_info
print('Platform:', get_platform_info())
print('Runtimes:', check_tflite_runtime())
"
```

### Validate Model Files
```bash
# Check that model files exist and are valid TFLite format
python3 -c "
from pathlib import Path
model_path = 'data/champions/test-app/rps_t10m/ppa_model.tflite'
with open(model_path, 'rb') as f:
    magic = f.read(4)
    if magic == b'TFL3':
        print(f'✓ {model_path} is valid TFLite format')
    else:
        print(f'✗ {model_path} has invalid magic bytes: {magic!r}')
"
```

---

## Phase 2: Deploy Updated Operator

### 1. Load the New Operator Image
```bash
# The image was just built with the fixes
docker image ls | grep ppa-operator

# Tag it if needed for your registry
docker tag ppa-operator:latest <your-registry>/ppa-operator:latest
```

### 2. Deploy to Kubernetes
```bash
# If you have the deployment manifest
kubectl apply -f deploy/operator-deployment.yaml

# Or scale the existing deployment with the new image
kubectl set image deployment/ppa-operator \
  ppa-operator=ppa-operator:latest \
  -n default
```

### 3. Wait for Pod to be Ready
```bash
# Watch the pod startup
kubectl get pods -l app=ppa-operator -n default -w

# Check if pod is running
kubectl get pods -l app=ppa-operator -n default -o wide
```

---

## Phase 3: Inspect Operator Logs

### View Recent Logs
```bash
# Stream logs from the operator pod
kubectl logs -f -l app=ppa-operator -n default

# View last 100 lines
kubectl logs -l app=ppa-operator -n default --tail=100
```

### What to Look For (Success Indicators)
```
✓ "[INFO] Model loaded via tflite_runtime"         # Runtime loaded successfully
✓ "[INFO] Loaded model from /models/..."           # Model files found and loaded
✓ "[INFO] Detected model lookback: XX"             # Model format recognized
✓ "[INFO] [default/test-app-ppa-rps-t10m] Timer 'reconcile' succeeded"  # Reconciliation worked
```

### What to Look For (Failure Indicators & Diagnostics)
```
✗ "[ERROR] Failed to load model/scaler: No TFLite runtime found"
   → Check tflite-runtime is installed in operator image
   → Run diagnostics section below

✗ "[ERROR] Failed to load model/scaler: [Errno 2] No such file or directory: '/models/...'"
   → Model file not found - check PVC mounting
   → Verify model was pushed correctly

✗ "[ERROR] Failed to load model/scaler: File corrupted"
   → Model file may have been truncated during transfer
   → Rerun: ppa model push
```

---

## Phase 4: Diagnostics - Deep Dive Debugging

### Option 1: Check Operator Pod Environment

```bash
# Get pod name
POD=$(kubectl get pods -l app=ppa-operator -n default -o jsonpath='{.items[0].metadata.name}')

# Enter the pod
kubectl exec -it $POD -n default -- bash

# Inside the pod, run diagnostics:
python3 << 'EOF'
from ppa.operator.diagnostics import (
    diagnose_model_load_issue, 
    print_diagnostics,
    check_tflite_runtime
)

# Check runtimes available
print("=== TFLite Runtimes ===")
print(check_tflite_runtime())

# Run full diagnostics for a specific model
print("\n=== Full Diagnostics ===")
report = diagnose_model_load_issue(
    model_path="/models/test-app/rps_t10m/ppa_model.tflite",
    scaler_path="/models/test-app/rps_t10m/scaler.pkl",
    target_scaler_path="/models/test-app/rps_t10m/target_scaler.pkl"
)
print_diagnostics(report)
EOF
```

### Option 2: Manual Model Load Test

```bash
# Inside the operator pod
python3 << 'EOF'
import sys
sys.path.insert(0, "/app")

from ppa.operator.predictor import Predictor

# Try to create a predictor (will trigger model loading)
try:
    predictor = Predictor(
        model_path="/models/test-app/rps_t10m/ppa_model.tflite",
        scaler_path="/models/test-app/rps_t10m/scaler.pkl",
        target_scaler_path="/models/test-app/rps_t10m/target_scaler.pkl"
    )
    print(f"✓ Model loaded successfully")
    print(f"  Lookback steps: {predictor.lookback}")
    print(f"  Ready: {predictor.ready()}")
except Exception as e:
    print(f"✗ Model loading failed: {e}")
    import traceback
    traceback.print_exc()
EOF
```

### Option 3: Check PVC Contents

```bash
# List model files on PVC
kubectl exec -it $POD -n default -- find /models -type f -name "*.tflite" -o -name "*.pkl"

# Check file sizes (unexpected 0 bytes = corruption)
kubectl exec -it $POD -n default -- ls -lah /models/test-app/rps_t10m/

# Verify model file magic bytes
kubectl exec -it $POD -n default -- python3 << 'EOF'
with open("/models/test-app/rps_t10m/ppa_model.tflite", "rb") as f:
    magic = f.read(4)
    if magic == b"TFL3":
        print("✓ Model has valid TFLite magic bytes")
    else:
        print(f"✗ Invalid magic: {magic!r}")
EOF
```

---

## Phase 5: Check Predictor Status

### View CRD Status
```bash
# List all PredictiveAutoscaler CRs
kubectl get predictiveautoscalers -n default

# Get details of a specific CR
kubectl get predictiveautoscaler test-app-ppa-rps-t10m -n default -o yaml

# Look for these status indicators:
# status:
#   model_loaded: true          ← Should be true
#   load_failures: 0            ← Should be 0
#   last_prediction_time: ...   ← Should have recent timestamp
```

### Verify Predictions Are Being Made
```bash
# Check operator metrics
kubectl logs -l app=ppa-operator -n default | grep -i "prediction"

# Expected pattern:
# [INFO] [default/test-app-ppa-rps-t10m] Prediction: 42.5 RPS
```

---

## Phase 6: Model Push Validation

### Re-push Models (if needed)
```bash
# With validation, the push command now checks model format
ppa model push \
  --app-name test-app \
  --namespace default \
  --horizon rps_t3m,rps_t5m,rps_t10m

# Expected output:
# ✓ Using champion artifacts for rps_t10m
# ✓ Model file validation: OK
# ✓ Copied rps_t10m/ppa_model.tflite
# ✓ Copied rps_t10m/scaler.pkl
```

### Validate Push Result
```bash
# List files that were pushed
kubectl exec -it $POD -n default -- ls -lah /models/test-app/

# Verify all 3 horizons are present
for horizon in rps_t3m rps_t5m rps_t10m; do
  kubectl exec -it $POD -n default -- test -f /models/test-app/$horizon/ppa_model.tflite && \
    echo "✓ $horizon/ppa_model.tflite exists" || \
    echo "✗ $horizon/ppa_model.tflite MISSING"
done
```

---

## Phase 7: Performance Verification

### Check Inference Latency
```bash
# From operator logs, look for inference time metrics
kubectl logs -l app=ppa-operator -n default | grep -i "inference"

# Expected pattern:
# [DEBUG] Slow inference: 45.2ms (expected <100ms)
```

### Monitor Metrics
```bash
# If you have Prometheus set up
kubectl port-forward -n default svc/prometheus 9090:9090

# Query in browser: http://localhost:9090
# Look for metrics:
# - ppa_model_load_failed (should be 0)
# - ppa_predictions_total (should be increasing)
# - ppa_inference_duration_seconds (should be < 0.1s)
```

---

## Common Issues & Solutions

### Issue 1: "No TFLite runtime found"
```
Root cause: tflite-runtime not installed in container
Solution:
1. Verify requirements.operator.txt has: tflite-runtime>=2.14.0
2. Rebuild docker image: docker build -f src/ppa/operator/Dockerfile -t ppa-operator:latest .
3. Deploy new image: kubectl set image deployment/ppa-operator ppa-operator=ppa-operator:latest
4. Check: kubectl logs -l app=ppa-operator | grep "Model loaded via"
```

### Issue 2: "Model file not found"
```
Root cause: PVC not mounted or model files not pushed
Solution:
1. Check PVC mounting: kubectl get pvc -n default
2. Verify mount path: kubectl exec $POD -- mount | grep /models
3. Rerun push: ppa model push --app-name test-app --namespace default
4. Verify: kubectl exec $POD -- ls -la /models/test-app/
```

### Issue 3: "Feature column mismatch"
```
Root cause: Model trained with different features than operator expects
Solution:
1. Retrain model: ppa model train --app-name test-app
2. Validate metadata: cat data/champions/test-app/rps_t10m/ppa_model_metadata.json
3. Repush: ppa model push --app-name test-app
4. Restart operator pod: kubectl delete pods -l app=ppa-operator -n default
```

### Issue 4: "Scaler file corrupted"
```
Root cause: Scaler was not regenerated correctly on push
Solution:
1. Delete old scaler: rm data/champions/test-app/*/scaler.pkl
2. Retrain: ppa model train --app-name test-app
3. Promote: ppa model promote --app-name test-app
4. Repush: ppa model push --app-name test-app
```

---

## Rollback Plan

If you need to revert to the previous operator version:

```bash
# Scale down current operator
kubectl scale deployment ppa-operator --replicas=0 -n default

# Use previous image (if available)
kubectl set image deployment/ppa-operator \
  ppa-operator=ppa-operator:previous \
  -n default

# Scale back up
kubectl scale deployment ppa-operator --replicas=1 -n default
```

---

## Next Steps After Successful Verification

Once all tests pass:

1. **Update CI/CD Pipeline**: Push image to your registry
   ```bash
   docker tag ppa-operator:latest <registry>/ppa-operator:latest
   docker push <registry>/ppa-operator:latest
   ```

2. **Update Deployment Manifest**: Reference the new image
   ```yaml
   image: <registry>/ppa-operator:latest
   ```

3. **Documentation**: Update runbooks with new diagnostics commands

4. **Monitoring**: Set up alerts for model_load_failed metrics

---

## Support & Debugging

For additional help, check:
- Operator logs: `kubectl logs -l app=ppa-operator -n default`
- CRD status: `kubectl get predictiveautoscalers -n default -o yaml`
- PVC contents: `kubectl exec $POD -- find /models -type f`
- Diagnostics: Run Python diagnostics scripts above
