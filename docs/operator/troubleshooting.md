# Operator Troubleshooting Guide

**Common issues, error messages, diagnostic steps, and solutions**

---

## Pod Issues

### Pod in CrashLoopBackOff

**Symptom:** Pod restarts repeatedly, never reaches Running state

**Diagnosis:**
```bash
# Check pod status
kubectl describe pod -l app=ppa-operator

# Check recent logs (just before crash)
kubectl logs deployment/ppa-operator --previous
```

**Common causes & fixes:**

| Error Message | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: sklearn` | scikit-learn missing from requirements.txt | Use `ppa_redeploy.sh` or rebuild with updated requirements.txt |
| `_pickle.UnpicklingError: STACK_GLOBAL` | Scalers saved with Python 3.13+numpy 2.x, pod has Python 3.11+numpy 1.x | Use `ppa_redeploy.sh` which regenerates scalers in pod |
| `bind: Permission denied` | Trying to bind to port < 1024 | Remove any port binds from dockerfile, use only pod-to-pod communication |
| `RBAC: ..ppa/predictiveautoscalers is forbidden` | ServiceAccount missing roles | Reapply RBAC: `kubectl apply -f deploy/rbac.yaml` |

---

## Prometheus Connectivity

### CR stuck "Warming up" after 25 minutes

**Symptom:** Logs show `Warming up: N/24 steps collected` but counter doesn't advance (should be 24 steps = ~12 min)

**Diagnosis:**
```bash
# Check operator logs for Prometheus errors
kubectl logs deployment/ppa-operator | grep -i prometheus

# Verify Prometheus is reachable from pod
kubectl exec deployment/ppa-operator -- \
  curl -v http://prometheus:9090/-/ready

# Check Prometheus URL in operator pod
kubectl exec deployment/ppa-operator -- env | grep PROMETHEUS
```

**Common causes & fixes:**

| Issue | Check | Fix |
|---|---|---|
| `Connection refused` | Prometheus not running | `kubectl get svc prometheus` or start Prometheus stack |
| `Name or service not known` | DNS resolution failed | Check Prometheus service DNS name, use full FQDN |
| `timeout` | Network latency | Increase `PROMETHEUS_TIMEOUT`: `kubectl set env deployment/ppa-operator PROMETHEUS_TIMEOUT=30` |

**Test connectivity:**
```bash
# Port-forward and test locally
kubectl port-forward svc/prometheus 9090:9090 &
curl http://localhost:9090/api/v1/targets
```

---

## Model & Scaler File Issues

### Error: "File not found: /models/.../ppa_model.tflite"

**Symptom:** Operator can't load model, CR skipped

**Solution:** Use `ppa_redeploy.sh` to handle model loading properly
```bash
./scripts/ppa_redeploy.sh  # Automatically copies models to PVC
```

**Manual diagnosis (if troubleshooting):**
```bash
# Check if file exists
kubectl exec deployment/ppa-operator -- \
  ls -la /models/test-app/ppa_model.tflite

# Check PVC mount
kubectl describe pod -l app=ppa-operator | grep -A 5 Mounts

# Check file transfer completed
kubectl exec deployment/ppa-operator -- \
  stat /models/test-app/ppa_model.tflite
```

**Manual solutions:**

1. **Copy model to PVC (recommended: use ppa_redeploy.sh):**
   ```bash
   # Create temporary pod with PVC access
   kubectl run -it --rm --image=busybox --restart=Never \
     -v models-pvc:/models -- sh
   
   # If using minikube:
   minikube mount /local/path/to/models:/mnt/models
   ```

2. **Verify file integrity:**
   ```bash
   # Check file size is reasonable (should be ~100KB+)
   kubectl exec deployment/ppa-operator -- \
     du -h /models/test-app/ppa_model.tflite
   
   # Should output > 100K, not 0 bytes
   ```

3. **Check model path in CR:**
   ```bash
   kubectl get ppa test-app-ppa -o jsonpath='{.spec.modelPath}'
   # Should match: /models/<app>/ppa_model.tflite
   ```

---

## TFLite Model Loading

### Error: "Didn't find op for builtin opcode 'FULLY_CONNECTED' version 12..."

**Root Cause:** Numpy version mismatch between training and runtime

**Solutions:**

1. **Update numpy in operator pod (immediate):**
   ```bash
   kubectl exec deployment/ppa-operator -- \
     pip install --force-reinstall numpy==1.26.4
   ```

2. **Fix permanently in Docker image:**
   ```bash
   # Ensure operator/requirements.txt has:
   # numpy==1.26.4
   
   # Rebuild image:
   docker build -t ppa-operator:latest -f operator/Dockerfile . --no-cache
   kubectl rollout restart deployment/ppa-operator
   ```

### Error: "numpy.core.multiarray failed to import"

**Cause:** Numpy 2.x incompatibility with tflite_runtime binary

**Fix:**
```bash
# Downgrade numpy immediately
kubectl exec deployment/ppa-operator -- \
  pip install numpy==1.26.4
```

### Error: "Cannot load tflite_runtime: no module named 'tflite_runtime'"

**Cause:** tflite_runtime binary is a native compiled module, Python fallback not available

**Fix:**

1. **Install TensorFlow (has builtin TFLite):**
   ```bash
   kubectl exec deployment/ppa-operator -- \
     pip install tensorflow==2.20.0
   ```

2. **Verify tensorflow.lite works:**
   ```bash
   kubectl exec deployment/ppa-operator -- python3 -c \
     "import tensorflow as tf; print('✓ TensorFlow available')"
   ```

---

## Scaling Not Happening

### Replicas not changing despite predictions

**Symptom:** CR shows predictions but deployment replicas stay constant

**Diagnosis:**
```bash
# Check CR predictions
kubectl get ppa test-app-ppa -o yaml | grep -A 10 lastPrediction

# Check if operator is actually patching
kubectl logs deployment/ppa-operator | grep -i "patch\|PATCH"

# Check deployment patch history
kubectl describe deployment test-app | grep -A 5 Events
```

**Common causes & fixes:**

| Cause | Check | Fix |
|---|---|---|
| Stabilization filter holding back | `lastPrediction.reason` | Wait 2 cycles (60s) for confirmed prediction |
| Rate limits capping replica jump | Same | OK if max jump reached, wait next cycle |
| Operator RBAC missing deployment patch permission | `kubectl get clusterrole ppa-operator -o yaml` | Reapply RBAC: `kubectl apply -f deploy/rbac.yaml` |
| CR status.consecutiveSkips > 0 | `kubectl get ppa -o jsonpath='{.items[*].status.consecutiveSkips}'` | Check `consecutiveSkips` value, debug if >5 |

**Test manual patching:**
```bash
# If operator can't patch, test manually
kubectl patch deployment test-app -p '{"spec":{"replicas":5}}'

# If this works, operator permission might be restricted
```

---

## Performance Issues

### Operator pod high memory usage

**Symptom:** Pod using > 1GB memory, possible OOMKilled

**Diagnosis:**
```bash
# Check memory usage
kubectl top pod -l app=ppa-operator

# Check if OOMKilled
kubectl describe pod -l app=ppa-operator | grep OOMKilled

# Check number of CRs (scales memory usage)
kubectl get ppa --all-namespaces | wc -l
```

**Solutions:**

1. **Increase pod memory limit:**
   ```bash
   kubectl set resources deployment/ppa-operator \
     --limits=memory=2Gi
   ```

2. **Reduce number of CRs per operator:**
   - Deploy additional operator pods with label selectors
   - Split apps across multiple namespaces

3. **Reduce feature window (less history):**
   ```bash
   kubectl set env deployment/ppa-operator \
     PPA_LOOKBACK_STEPS=8  # 4 min instead of 6 min
   ```

### Operator slow to reconcile (cycle > 10 seconds)

**Symptom:** Operator logs show reconciliation taking too long

**Causes & fixes:**

| Metric | Issue | Fix |
|---|---|---|
| 100+ CRs per operator | Scaling bottleneck | Split across multiple operators |
| Large feature window | Memory/CPU intensive | Reduce `PPA_LOOKBACK_STEPS` |
| Slow Prometheus queries | Network/query plan issue | Optimize PromQL queries |
| TFLite model too large | Model inference slow | Use quantized model |

---

## CR/API Validation Errors

### Error: "spec.minReplicas must be less than spec.maxReplicas"

**Cause:** Invalid CR specification

**Fix:**
```bash
# Check values
kubectl get ppa test-app-ppa -o jsonpath='{.spec.minReplicas},{.spec.maxReplicas}'

# Fix CR
kubectl patch ppa test-app-ppa -p '{"spec":{"minReplicas":2,"maxReplicas":20}}'
```

### Error: "modelPath must end with .tflite"

**Cause:** Model file extension missing or wrong

**Fix:**
```bash
# Update CR spec
kubectl patch ppa test-app-ppa -p '{"spec":{"modelPath":"/models/test-app/ppa_model.tflite"}}'
```

---

## Incorrect Scaling Behavior

### Scaling too aggressive (constant flapping)

**Symptom:** Replicas oscillate up/down every 30 seconds

**Causes & fixes:**

1. **Stabilization window too short:**
   ```bash
   kubectl set env deployment/ppa-operator \
     PPA_STABILIZATION_STEPS=3  # Require 3 cycles agreement
   ```

2. **Rate limits too high:**
   ```bash
   kubectl patch ppa test-app-ppa -p '{"spec":{"scaleUpRate":1.5}}'
   ```

3. **Model predictions noisy:**
   - Retrain model with more data
   - Use longer feature window: `PPA_LOOKBACK_STEPS=16`

### Scaling too conservative (late response)

**Symptom:** Replicas scale up after traffic spike, not before

**Causes & fixes:**

1. **Prediction horizon too short:**
   - Use `rps_t5m` model instead of `rps_t3m`

2. **Stabilization window too long:**
   ```bash
   kubectl set env deployment/ppa-operator \
     PPA_STABILIZATION_STEPS=1  # Scale immediately
   ```

3. **Rate limits too restrictive:**
   ```bash
   kubectl patch ppa test-app-ppa \
     -p '{"spec":{"scaleUpRate":3.0}}'
   ```

---

## Debugging Checklist

```bash
# 1. Operator pod status
kubectl get pods -l app=ppa-operator
kubectl describe pod -l app=ppa-operator

# 2. CRs exist and valid
kubectl get ppa
kubectl get ppa test-app-ppa -o yaml

# 3. Prometheus reachable
kubectl logs deployment/ppa-operator | grep -i prometheus

# 4. Models exist in PVC
kubectl exec deployment/ppa-operator -- \
  ls -lh /models/test-app/

# 5. Model can load
kubectl exec deployment/ppa-operator -- python3 -c \
  "import tensorflow; i=tensorflow.lite.Interpreter('/models/test-app/ppa_model.tflite'); i.allocate_tensors(); print('✓')"

# 6. RBAC permissions OK
kubectl auth can-i patch deployments --as=system:serviceaccount:default:ppa-operator

# 7. Target deployment exists
kubectl get deployment test-app

# 8. Recent logs for actual error
kubectl logs deployment/ppa-operator --tail=50 | tail -20
```

---

## Getting Help

If still stuck, collect diagnostic info:

```bash
# Collect full diagnostic bundle
mkdir -p /tmp/ppa-debug
cd /tmp/ppa-debug

# Operator pod
kubectl get pod -l app=ppa-operator -o yaml > operator-pod.yaml
kubectl logs deployment/ppa-operator > operator-logs.txt
kubectl top pod -l app=ppa-operator > operator-resources.txt

# CRs
kubectl get ppa -o yaml > crs.yaml

# Prometheus status
kubectl exec deployment/ppa-operator -- \
  curl -s http://prometheus:9090/api/v1/targets > prometheus-targets.json

# RBAC
kubectl get clusterrole ppa-operator -o yaml > rbac-role.yaml

# Create tarball
tar czf ppa-debug.tar.gz *.yaml *.txt *.json
```

**Share this with debugging:**
```
- What's the symptom? (e.g., "remains CrashLoopBackOff")
- When did it start?
- Any recent changes to cluster/operator?
- kubectl version
- Kubernetes version
- Prometheus version
- Contents of /tmp/ppa-debug.tar.gz
```

---

## See Also

- **[Commands Reference](./commands.md)** — Useful diagnostic commands
- **[Architecture](./architecture.md)** — Understanding system behavior
- **[Configuration](./configuration.md)** — Tuning parameters

