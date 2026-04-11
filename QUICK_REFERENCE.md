# PPA Model System - Quick Reference

## The Problem

```
Error: RuntimeError: No TFLite runtime found (tried ai_edge_litert, tensorflow.lite, tflite_runtime)
```

**Root Cause:** Operator Dockerfile only installs `ai-edge-litert==2.1.3`. If that wheel isn't available for your platform or has issues, TFLite loading fails.

## The Flow

```
Train (Keras)
    ↓
Convert (TFLite)
    ↓
Promote (Champion Dir)
    ↓
Push (To PVC in K8s)
    ↓
Operator loads at `/models/{app}/{horizon}/`
    ↓ FAILS HERE if no TFLite runtime
Operator becomes useless (can't make scaling decisions)
```

## Key Files

| File | Purpose | Issue |
|------|---------|-------|
| `requirements.operator.txt` | Operator dependencies | Missing fallback TFLite runtime |
| `src/ppa/operator/Dockerfile` | Operator image | Only installs ai-edge-litert |
| `src/ppa/operator/predictor.py:121-169` | Model loading code | Hard failure if all runtimes unavailable |
| `src/ppa/operator/main.py:113-128` | Path resolution | Uses `/models/{app}/{horizon}/` convention |
| `src/ppa/cli/commands/push.py` | Model upload | Copies to PVC via loader pod |
| `src/ppa/runtime/regenerate_scalers.py` | Pickle regeneration | Re-fits scalers inside pod |

## Model Paths

### Development (Local)
```
data/artifacts/{app}/{namespace}/{horizon}/
├─ ppa_model_{horizon}.keras
├─ ppa_model_{horizon}.tflite
├─ scaler_{horizon}.pkl
└─ target_scaler_{horizon}.pkl
```

### Promotion (Local)
```
data/champions/{app}/{namespace}/{horizon}/
├─ ppa_model.tflite
├─ scaler.pkl
├─ target_scaler.pkl
└─ ppa_model_metadata.json
```

### Runtime (K8s PVC)
```
/models/{app}/{horizon}/
├─ ppa_model.tflite
├─ scaler.pkl
├─ target_scaler.pkl
└─ ppa_model_metadata.json
```

## Model Loading Sequence

1. **CRD Spec** specifies (or uses defaults):
   - `modelPath`: `/models/{app}/{horizon}/ppa_model.tflite`
   - `scalerPath`: `/models/{app}/{horizon}/scaler.pkl`
   - `targetScalerPath`: `/models/{app}/{horizon}/target_scaler.pkl`

2. **Operator starts** and reads CRD

3. **Predictor created** with those paths

4. **_try_load()** attempts:
   - `ai_edge_litert.Interpreter` ← FAILS if binary unavailable
   - `tensorflow.lite.Interpreter` ← FAILS (TensorFlow not installed)
   - `tflite_runtime.interpreter.Interpreter` ← FAILS (not in requirements)

5. **Error raised** → Operator degraded

## Quick Fixes (In Priority Order)

### Fix 1: Add tflite-runtime (IMMEDIATE)
```bash
# Edit requirements.operator.txt
# Add: tflite-runtime>=2.14.0

# Rebuild operator image:
docker build -f src/ppa/operator/Dockerfile -t ppa-operator:latest .
minikube image load ppa-operator:latest  # If using minikube
```

### Fix 2: Verify PVC Setup
```bash
# Check PVC exists and models are mounted
kubectl get pvc
kubectl exec -it deployment/ppa-operator -- ls -la /models/

# Should see: /models/{app_name}/{horizon}/ppa_model.tflite
```

### Fix 3: Check CRD Config
```bash
# Get CR to see model paths:
kubectl get predictiveautoscaler test-app-ppa -o yaml

# Should show spec.modelPath pointing to /models/...
```

### Fix 4: Check Operator Logs
```bash
# See what failed:
kubectl logs deployment/ppa-operator | grep -i tflite

# Look for: "No TFLite runtime found"
```

## Model Promotion Workflow

### 1. Train locally
```bash
ppa model train --csv data/training-data/training_data_v2.csv --target rps_t3m
```

### 2. Convert to TFLite
```bash
ppa model convert --app-name test-app --target rps_t3m
```

### 3. Push to K8s
```bash
ppa model push --app-name test-app --horizon rps_t3m
```

**What push.py does:**
1. Creates loader pod in K8s
2. Copies TFLite model to pod: `/tmp/ppa_model.tflite`
3. Copies training CSV to pod: `/tmp/training_data.csv`
4. Runs `regenerate_scalers.py` inside pod (fixes pickle compatibility)
5. Copies regenerated scalers back: `/models/{app}/{horizon}/scaler.pkl`
6. Deletes loader pod

## Debugging Checklist

- [ ] Operator pod running? `kubectl get pods`
- [ ] Operator logs show TFLite error? `kubectl logs deployment/ppa-operator`
- [ ] TFLite runtime installed in pod? `kubectl exec -it deployment/ppa-operator -- python -c "import ai_edge_litert; print('OK')"`
- [ ] Model file exists on PVC? `kubectl exec -it deployment/ppa-operator -- ls /models/test-app/rps_t3m/`
- [ ] Metadata file valid? `kubectl exec -it deployment/ppa-operator -- cat /models/test-app/rps_t3m/ppa_model_metadata.json`
- [ ] Scalers loadable? `kubectl exec -it deployment/ppa-operator -- python -c "import joblib; joblib.load('/models/test-app/rps_t3m/scaler.pkl')"`

## Testing Model Load

```bash
# SSH into operator pod and test:
kubectl exec -it deployment/ppa-operator -- bash

# Inside pod:
cd /app
python3 << 'PYTHON'
from ppa.operator.predictor import Predictor
try:
    p = Predictor(
        "/models/test-app/rps_t3m/ppa_model.tflite",
        "/models/test-app/rps_t3m/scaler.pkl"
    )
    if p.ready():
        print("✓ Model loaded successfully!")
    else:
        print("✗ Model not ready (insufficient history)")
except Exception as e:
    print(f"✗ Error: {e}")
PYTHON
```

## Architecture Decision: Why These Paths?

- **`/models/{app}/{horizon}/`**: Namespaces models by app + prediction horizon
- **`ppa_model.tflite`**: Canonical name (not app/horizon-specific) for easy operator reference
- **`scaler.pkl` + `target_scaler.pkl`**: Feature scaling (input) and target scaling (output)
- **`ppa_model_metadata.json`**: Schema validation to catch train/serve mismatches early

## Backoff Strategy (PR#10)

If TFLite runtime isn't found, Predictor enters retry mode:

```
Attempt 1: Immediate fail
Attempt 2: Wait 2^1 = 2 sec
Attempt 3: Wait 2^2 = 4 sec
...
Attempt 10: Wait 2^10 = 1024 sec ≈ 17 min (capped at 5 min)
Attempt 11+: Give up (log critical)
```

This allows for "install TFLite runtime later" recovery scenarios.

## Metadata Validation (PR#7, PR#8)

When model loads, Predictor checks:

**CRITICAL (fail immediately):**
- `feature_columns` in metadata must match operator's `FEATURE_COLUMNS`

**WARNINGS (log but continue):**
- `lookback` mismatch (different history length expected)
- `accuracy_loss_pct` > 5% (quantization degradation)
- Missing metadata file (backward compat mode)

## History Preservation on Model Upgrade (PR#5)

When CR spec model path changes:

1. Operator detects `!paths_match()` → True
2. Snapshots old model's history (up to 60 steps ≈ 30 min)
3. Creates new Predictor with new model
4. Restores history into new predictor
5. Avoids 30-min "coldstart" warmup

**Benefit:** Smooth model upgrades without scaling blindness

---

## Related PRs

- **PR#5**: History preservation on model upgrade
- **PR#7, PR#8**: Metadata validation for schema safety
- **PR#10**: Exponential backoff for model load failures
- **PR#12**: Concept drift detection
- **PR#13**: Inference latency tracking
- **PR#15**: History serialization in CR status
- **PR#16**: Retraining trigger logic
