# Operator Commands Reference

**Useful kubectl and diagnostic commands for managing and monitoring the operator**

---

## Quick Deployment (Recommended)

For most deployments, use the automated script instead of manual steps:

```bash
# Full pipeline: retrain + convert + deploy + warmup
./scripts/ppa_redeploy.sh --retrain --epochs 100

# Deploy existing champion
./scripts/ppa_redeploy.sh

# Fast iteration (skip Docker rebuild)
./scripts/ppa_redeploy.sh --skip-build

# See what it does
./scripts/ppa_redeploy.sh --help
```

For step-by-step manual deployment, see [Deployment Guide](./deployment.md).

---

## Operator Pod Management

### Check operator status

```bash
# Pod status
kubectl get pods -l app=ppa-operator

# Detailed pod info
kubectl describe pod -l app=ppa-operator

# Pod logs
kubectl logs deployment/ppa-operator

# Stream logs (follow mode)
kubectl logs -f deployment/ppa-operator

# Last 100 lines
kubectl logs deployment/ppa-operator --tail=100

# Last 5 minutes
kubectl logs deployment/ppa-operator --since=5m

# Filter for specific app
kubectl logs deployment/ppa-operator | grep test-app-ppa
```

### Monitor operator in real-time

```bash
# Watch operator events
kubectl logs -f deployment/ppa-operator --all-containers

# Watch pod status
kubectl get pods -l app=ppa-operator -w

# Watch deployment status
kubectl get deployment ppa-operator -w
```

### Restart operator

```bash
# Graceful restart (recreate pod)
kubectl rollout restart deployment/ppa-operator

# Wait for roll out
kubectl rollout status deployment/ppa-operator

# Force delete pod (if hung)
kubectl delete pod -l app=ppa-operator --grace-period=0 --force
```

---

## Custom Resource Management

### List all CRs

```bash
# All CRs in default namespace
kubectl get ppa

# All CRs across all namespaces
kubectl get ppa --all-namespaces

# With wide output (more details)
kubectl get ppa -o wide

# As YAML
kubectl get ppa -o yaml

# As JSON
kubectl get ppa -o json
```

### Inspect CR details

```bash
# Describe CR
kubectl describe ppa test-app-ppa

# Full YAML
kubectl get ppa test-app-ppa -o yaml

# Just status
kubectl get ppa test-app-ppa -o jsonpath='{.status}' | python3 -m json.tool

# Watch CR changes
kubectl get ppa test-app-ppa -w
```

### Edit CR

```bash
# Interactive edit
kubectl edit ppa test-app-ppa

# Patch single field
kubectl patch ppa test-app-ppa -p '{"spec":{"maxReplicas":30}}'

# Patch JSON patch
kubectl patch ppa test-app-ppa --type merge -p '{"spec":{"scaleUpRate":3.0}}'
```

### Create CR from template

```bash
# Save template
kubectl get ppa test-app-ppa -o yaml > my-ppa-template.yaml

# Edit and reapply
kubectl apply -f my-ppa-template.yaml
```

---

## Target Deployment Monitoring

### Watch deployment replicas

```bash
# Real-time replica changes
kubectl get deployment my-app -w

# Just replica count
watch kubectl get deployment my-app

# JSON output (programmatic)
kubectl get deployment my-app -o jsonpath='{.status.replicas}'
```

### Check deployment events

```bash
# Events for deployment
kubectl describe deployment my-app

# Just events
kubectl get events --field-selector involvedObject.name=my-app

# Watch events
kubectl get events --watch
```

---

## Logs & Diagnostics

### Operator logs with timestamps

```bash
# Include timestamps
kubectl logs deployment/ppa-operator -f --timestamps=true

# Parse and pretty-print JSON logs
kubectl logs deployment/ppa-operator | python3 -m json.tool 2>/dev/null | jq .
```

### Search logs for errors

```bash
# Find all error/warning lines
kubectl logs deployment/ppa-operator | grep -E "ERROR|WARN"

# Find specific app errors
kubectl logs deployment/ppa-operator | grep -E "test-app.*ERROR"

# Count errors
kubectl logs deployment/ppa-operator | grep "ERROR" | wc -l
```

### Extract prediction data

```bash
# Find all predictions
kubectl logs deployment/ppa-operator | grep "Predicting\|Prediction"

# Parse RPS predictions
kubectl logs deployment/ppa-operator | grep "RPS/Pod=" | tail -10
```

---

## Prometheus Integration

### Test Prometheus connectivity

```bash
# From operator pod
kubectl exec deployment/ppa-operator -- \
  curl -s http://prometheus:9090/-/ready

# Query Prometheus from pod
kubectl exec deployment/ppa-operator -- \
  curl -s 'http://prometheus:9090/api/v1/query?query=up'
```

### Port-forward to Prometheus

```bash
# Access Prometheus UI locally
kubectl port-forward svc/prometheus-operated 9090:9090

# View http://localhost:9090
```

---

## Model & Scaler Files

### Check files in PVC

```bash
# List models directory
kubectl exec -it deployment/ppa-operator -- ls -lh /models/

# List app-specific models
kubectl exec -it deployment/ppa-operator -- ls -lh /models/test-app/

# Verify files exist
kubectl exec deployment/ppa-operator -- \
  test -f /models/test-app/ppa_model.tflite && echo "✓ Model exists" || echo "✗ Model missing"
```

### Copy models to/from PVC

```bash
# Copy model from PVC to local
kubectl cp deployment/ppa-operator:/models/test-app/ppa_model.tflite ./model.tflite

# Copy model from local to PVC
kubectl cp ./ppa_model.tflite deployment/ppa-operator:/models/test-app/

# Verify integrity
md5sum ppa_model.tflite
kubectl exec deployment/ppa-operator -- md5sum /models/test-app/ppa_model.tflite
```

---

## Testing & Validation

### Test model loading

```bash
# Check if TFLite model can load
kubectl exec deployment/ppa-operator -- python3 -c "
import tensorflow as tf
i = tf.lite.Interpreter('/models/test-app/ppa_model.tflite')
i.allocate_tensors()
print('✓ Model loads successfully')
"
```

### Test feature collection

```bash
# Run feature collection manually
kubectl exec deployment/ppa-operator -- python3 -c "
import sys
sys.path.insert(0, '/app')
from operator.features import fetch_prometheus_metrics
metrics = fetch_prometheus_metrics('prometheus:9090', 'default', '{app=test}')
print(f'✓ Fetched {len(metrics)} metrics')
"
```

### Simulate prediction cycle

```bash
# Test full reconciliation cycle for an app
kubectl exec deployment/ppa-operator -- python3 << 'EOF'
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

response = os.system('python3 -c """
import sys
sys.path.insert(0, '/app')
from operator.scaler import Scaler
s = Scaler(capacity_per_pod=80, min_replicas=2, max_replicas=20)
replicas = s.calculate_replicas(predicted_rps=1200)
print(f'✓ Scaled to {replicas} replicas for 1200 RPS')
"""')
EOF
```

---

## Performance Monitoring

### CPU & memory usage

```bash
# Current resource usage
kubectl top pod -l app=ppa-operator

# Watch resource trends
kubectl top pod -l app=ppa-operator --containers

# All pods in namespace
kubectl top pods
```

### Check CRD schema

```bash
# View CRD definitions
kubectl get crd predictiveautoscalers.ppa.example.com -o yaml

# Validate CRD
kubectl get crd predictiveautoscalers.ppa.example.com -o jsonpath='{.spec.validation.openAPIV3Schema}' | python3 -m json.tool
```

---

## Advanced Debugging

### Debug operator initialization

```bash
# Start operator with debug logging
kubectl set env deployment/ppa-operator LOG_LEVEL=DEBUG
kubectl rollout restart deployment/ppa-operator
kubectl logs -f deployment/ppa-operator | grep DEBUG
```

### Trace reconciliation cycle

```bash
# Extract all reconciliation steps for one CR
kubectl logs deployment/ppa-operator \
  | grep -A 10 "test-app-ppa" \
  | head -50
```

### Check operator state

```bash
# Exec into pod and inspect state
kubectl exec -it deployment/ppa-operator -- bash

# Inside pod:
cd /app
python3 -c "
import main
# Inspect operator state (if available via API)
"
```

### Mock Prometheus response

```bash
# If Prometheus is down, test operator resilience
kubectl exec -it deployment/ppa-operator -- bash

# Simulate metric collection
python3 << 'EOF'
import os
os.environ['PROMETHEUS_URL'] = 'http://invalid:9999'
# Try to fetch metrics (should fail gracefully)
from operator.features import fetch_prometheus_metrics
try:
    metrics = fetch_prometheus_metrics('http://invalid:9999', 'default', 'selector')
except Exception as e:
    print(f'✓ Gracefully handled error: {e}')
EOF
```

---

## One-Liners

### Check all CRs status

```bash
kubectl get ppa -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.lastPrediction.predictedRPS}{"\t"}{.status.lastPrediction.desiredReplicas}{"\n"}{end}'
```

### Count CRs by namespace

```bash
kubectl get ppa --all-namespaces | tail -n +2 | awk '{print $1}' | sort | uniq -c
```

### Find CRs with errors

```bash
kubectl get ppa -o jsonpath='{range .items[?(@.status.consecutiveSkips>0)]}{.metadata.name}{"\n"}{end}'
```

### Check if all models exist

```bash
for cr in $(kubectl get ppa -o jsonpath='{.items[*].metadata.name}'); do
  model=$(kubectl get ppa $cr -o jsonpath='{.spec.modelPath}')
  echo -n "$cr: "
  kubectl exec deployment/ppa-operator -- test -f "$model" && echo "✓" || echo "✗"
done
```

### Monitor scaling events

```bash
# Watch logs for scaling decisions
kubectl logs -f deployment/ppa-operator | grep -E "Scale|Replica|PATCH"
```

---

## Useful Aliases

Add to your `.bashrc` or `.zshrc`:

```bash
# Operator shortcuts
alias kppa='kubectl get ppa'
alias kppaw='kubectl get ppa -w'
alias kppal='kubectl logs -f deployment/ppa-operator'
alias kppae='kubectl exec -it deployment/ppa-operator -- bash'

# Watch target app
alias kwapp='kubectl get deployment test-app -w'

# Check everything
check_ppa() {
  echo "=== Operator ==="
  kubectl get pods -l app=ppa-operator
  echo -e "\n=== CRs ==="
  kubectl get ppa
  echo -e "\n=== Errors ==="
  kubectl logs deployment/ppa-operator | grep -E "ERROR|WARN" | tail -5
}

# Quick restart
restart_ppa() {
  kubectl rollout restart deployment/ppa-operator
  kubectl rollout status deployment/ppa-operator
}
```

---

## See Also

- **[Troubleshooting](./troubleshooting.md)** — Common issues & solutions
- **[Configuration](./configuration.md)** — Environment variables for logging/tuning
- **[Architecture](./architecture.md)** — Understanding logs and events

