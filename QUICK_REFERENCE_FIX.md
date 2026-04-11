# Quick Reference: Model Upload Fixes

## What Was Fixed

| Component | Issue | Fix |
|-----------|-------|-----|
| **TFLite Runtime** | Only `ai_edge_litert` available, no fallback | Added `tflite-runtime>=2.14.0` + better fallback logic |
| **Predictor.py** | `predict()` method broken (wrong indentation) | Restructured entire file - methods now in class |
| **Error Messages** | Vague "No TFLite found" errors | Added diagnostics module with detailed debugging |
| **Model Push** | Could push corrupted models silently | Added TFLite format validation |
| **Loader Image** | Using generic Python image | Created optimized multi-stage Dockerfile |

---

## Deploy in 3 Steps

### Step 1: Use New Operator Image
```bash
# The image has been rebuilt with all fixes
docker images | grep ppa-operator:latest
```

### Step 2: Deploy to Kubernetes
```bash
kubectl set image deployment/ppa-operator \
  ppa-operator=ppa-operator:latest \
  -n default
```

### Step 3: Verify Success
```bash
# Watch pod startup
kubectl logs -f -l app=ppa-operator -n default

# Look for this message:
# [INFO] Model loaded via tflite_runtime
# [INFO] Loaded model from /models/...
```

---

## Testing Commands (Copy & Paste)

### Basic Tests
```bash
# Check if operator pod is running
kubectl get pods -l app=ppa-operator -n default

# View operator logs
kubectl logs -l app=ppa-operator -n default --tail=50

# Get pod name for other commands
POD=$(kubectl get pods -l app=ppa-operator -n default -o jsonpath='{.items[0].metadata.name}')
```

### Diagnostics (Inside Pod)
```bash
# Enter the pod
kubectl exec -it $POD -n default -- bash

# Inside pod - check TFLite runtimes
python3 -c "
from ppa.operator.diagnostics import check_tflite_runtime
import json
print(json.dumps(check_tflite_runtime(), indent=2))
"

# Inside pod - full diagnostics
python3 -c "
from ppa.operator.diagnostics import diagnose_model_load_issue, print_diagnostics
report = diagnose_model_load_issue(
    '/models/test-app/rps_t10m/ppa_model.tflite',
    '/models/test-app/rps_t10m/scaler.pkl',
    '/models/test-app/rps_t10m/target_scaler.pkl'
)
print_diagnostics(report)
"

# Inside pod - test model loading
python3 << 'EOF'
from ppa.operator.predictor import Predictor
p = Predictor(
    '/models/test-app/rps_t10m/ppa_model.tflite',
    '/models/test-app/rps_t10m/scaler.pkl',
    '/models/test-app/rps_t10m/target_scaler.pkl'
)
print(f"✓ Model loaded")
print(f"  Ready: {p.ready()}")
print(f"  Lookback: {p.lookback}")
EOF
```

### File Verification
```bash
# Check model files exist on PVC
kubectl exec -it $POD -n default -- ls -lah /models/test-app/rps_t10m/

# Verify model has valid TFLite format
kubectl exec -it $POD -n default -- python3 << 'EOF'
with open("/models/test-app/rps_t10m/ppa_model.tflite", "rb") as f:
    magic = f.read(4)
    if magic == b"TFL3":
        print("✓ Valid TFLite model")
    else:
        print(f"✗ Invalid format: {magic!r}")
EOF
```

### Monitor Predictions
```bash
# Check if predictions are being made
kubectl logs -l app=ppa-operator -n default | grep "Prediction" | tail -5

# Check CR status
kubectl get predictiveautoscaler test-app-ppa-rps-t10m -n default -o yaml | grep -A 5 "status:"
```

---

## Troubleshooting

### Problem: "No TFLite runtime found"
```bash
# Check runtimes in operator container
kubectl exec $POD -n default -- python3 -c "
import sys
try:
    import tflite_runtime
    print('✓ tflite_runtime available')
except:
    print('✗ tflite_runtime NOT found')

try:
    import ai_edge_litert
    print('✓ ai_edge_litert available')
except:
    print('✗ ai_edge_litert NOT found')
"

# Solution: Rebuild operator image
docker build -f src/ppa/operator/Dockerfile -t ppa-operator:latest .
kubectl set image deployment/ppa-operator ppa-operator=ppa-operator:latest -n default
```

### Problem: "Model file not found"
```bash
# Check files on PVC
kubectl exec $POD -n default -- find /models -name "*.tflite" -o -name "*.pkl"

# Solution: Re-push models
ppa model push --app-name test-app --namespace default

# Verify they copied
kubectl exec $POD -n default -- ls -la /models/test-app/rps_t10m/
```

### Problem: "Feature column mismatch"
```bash
# Check model metadata
kubectl exec $POD -n default -- cat /models/test-app/rps_t10m/ppa_model_metadata.json

# Solution: Retrain model
ppa model train --app-name test-app
ppa model promote --app-name test-app
ppa model push --app-name test-app --namespace default

# Restart operator
kubectl delete pods -l app=ppa-operator -n default
```

---

## Files Modified

```
✏️  MODIFIED:
   └─ src/ppa/operator/predictor.py         (Restructured, added diagnostics import)
   └─ src/ppa/cli/commands/push.py          (Added model validation)

✨ NEW:
   └─ src/ppa/operator/diagnostics.py       (Debugging module)
   └─ src/ppa/loader/Dockerfile             (Optimized loader image)
   └─ TESTING_GUIDE.md                      (Comprehensive testing procedures)
   └─ IMPLEMENTATION_SUMMARY.md             (Full technical details)
```

---

## Expected Results After Fix

**Before**:
```
[ERROR] Failed to load model/scaler: No TFLite runtime found
[WARNING] [test-app-ppa-rps-t10m] Model not loaded
```

**After**:
```
[INFO] Model loaded via tflite_runtime
[INFO] Loaded model from /models/test-app/rps_t10m/ppa_model.tflite
[INFO] Detected model lookback: 60
[INFO] [test-app-ppa-rps-t10m] Timer 'reconcile' succeeded
```

---

## Next Steps

1. ✅ **Deploy**: Run Step 1-3 above
2. ✅ **Verify**: Run Testing Commands
3. ✅ **Monitor**: Watch logs for success messages
4. 📋 **Document**: Update your runbooks
5. 🔄 **CI/CD**: Push image to registry if verified

---

## Support Resources

- **Testing Guide**: See `TESTING_GUIDE.md` for complete procedures
- **Technical Details**: See `IMPLEMENTATION_SUMMARY.md` for architecture
- **Diagnostics**: Run diagnostics commands above for debugging
- **Logs**: `kubectl logs -l app=ppa-operator -n default`
