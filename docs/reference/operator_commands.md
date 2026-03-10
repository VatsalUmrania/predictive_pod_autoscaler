# Operator Commands Reference

**Predictive Pod Autoscaler | Operator Deployment & Debugging**

---

## Quick Start — Deploy to Minikube

```bash
# One-command deployment (from repo root)
./scripts/deploy_operator.sh --horizon rps_t10m

# Watch operator come up
kubectl logs -f deployment/ppa-operator

# Check operator health
kubectl get ppa
kubectl describe ppa test-app-ppa
```

---

## Detailed Deployment Steps

### Step 1: Apply CRD & RBAC

```bash
# Install CRD
kubectl apply -f deploy/crd.yaml

# Verify CRD schema includes all fields
kubectl explain ppa.spec
kubectl explain ppa.status

# Install RBAC (ServiceAccount, ClusterRole, ClusterRoleBinding)
kubectl apply -f deploy/rbac.yaml
```

### Step 2: Create PVC & Load Model Artifacts

```bash
# Create persistent volume for models
kubectl apply -f deploy/operator-deployment.yaml  # Creates PVC

# Method A: Direct kubectl cp (if PVC is mounted outside operator)
kubectl cp model/champions/rps_t10m/ppa_model.tflite \
  default/ppa-model-loader:/models/test-app/ppa_model.tflite

# Method B: Use the built-in loader (recommended — automated)
# The deploy script handles this automatically
```

**PVC Structure:**
```
/models/
└── test-app/
    ├── ppa_model.tflite
    ├── scaler.pkl
    └── target_scaler.pkl
```

### Step 3: Build & Deploy Operator Image

```bash
# Set up minikube docker env (one-time)
eval $(minikube docker-env)

# Build image locally
docker build -t ppa-operator:latest -f operator/Dockerfile .

# Verify image exists
docker images | grep ppa-operator

# Deploy operator pod
kubectl apply -f deploy/operator-deployment.yaml

# Check pod is ready and health probes passing
kubectl get pods -l app=ppa-operator
kubectl describe pod -l app=ppa-operator
```

### Step 4: Apply PredictiveAutoscaler CR

```bash
# Apply example CR for test-app
kubectl apply -f deploy/predictiveautoscaler.yaml

# Verify CR created
kubectl get ppa

# Watch CR status updates
kubectl get ppa -w

# Get detailed CR status
kubectl describe ppa test-app-ppa
```

---

## Operator Configuration (Environment Variables)

### Set Env Vars in Deployment

Edit `deploy/operator-deployment.yaml`:

```yaml
spec:
  template:
    spec:
      containers:
      - name: operator
        env:
        - name: PPA_TIMER_INTERVAL
          value: "30"        # Reconciliation cycle (seconds)
        - name: PPA_INITIAL_DELAY
          value: "60"        # Warmup before first cycle (seconds)
        - name: PPA_LOOKBACK_STEPS
          value: "12"        # Must match model training (12 × 30s = 6 min)
        - name: PPA_STABILIZATION_STEPS
          value: "2"         # Consecutive stable reads before scaling
        - name: PPA_PROM_FAILURE_THRESHOLD
          value: "10"        # Escalate to ERROR after N failures
        - name: PROMETHEUS_URL
          value: "http://prometheus-kube-prometheus-prometheus.monitoring:9090"
```

Then redeploy:
```bash
kubectl apply -f deploy/operator-deployment.yaml
kubectl rollout restart deployment/ppa-operator
```

---

## Monitoring & Status

### Check Operator Health

```bash
# Pod status
kubectl get pods -l app=ppa-operator

# Pod restarts (should be 0)
kubectl get pods -l app=ppa-operator -o wide

# Liveness/readiness probes
kubectl describe pod -l app=ppa-operator | grep -A 5 "Liveness"
```

### View CR Status

```bash
# Summary
kubectl get ppa -o wide

# Detailed status (includes lastPredictedLoad, currentReplicas, etc.)
kubectl get ppa test-app-ppa -o yaml

# Watch real-time updates (6-minute warmup, then updates every 30s)
kubectl get ppa test-app-ppa -w

# Check consecutiveSkips (Prometheus failures)
kubectl get ppa test-app-ppa -o jsonpath='{.status.consecutiveSkips}'
```

### View Operator Logs

```bash
# Last 100 lines
kubectl logs -l app=ppa-operator --tail=100

# Stream live (with timestamps)
kubectl logs -l app=ppa-operator -f --timestamps=true

# Last 5 minutes
kubectl logs -l app=ppa-operator --since=5m

# Filtered for scaling events
kubectl logs -l app=ppa-operator | grep "Scaling"

# Filtered for errors
kubectl logs -l app=ppa-operator | grep "ERROR"

# Filtered for Prometheus issues
kubectl logs -l app=ppa-operator | grep "Prometheus"
```

---

## Update Model/Scaler on PVC

### Push New Champion to Operator

```bash
# Option 1: Update CR paths → operator auto-reloads on next cycle
kubectl patch ppa test-app-ppa --type merge -p \
  '{"spec":{"modelPath":"/models/test-app/ppa_model.tflite"}}'

# Option 2: Direct replacement of files on PVC
# (Requires pod restart to detect — not recommended)

# Option 3: Use the pipeline with promotion
python model/pipeline.py \
  --csv data-collection/training-data/training_data_v2.csv \
  --horizons rps_t10m \
  --promote-if-better \
  --promote-cr-name test-app-ppa \
  --promote-cr-namespace default
# Pipeline patches CR automatically
```

### Verify Model Loaded

```bash
# Check operator logs for successful load
kubectl logs -l app=ppa-operator | grep "Loaded model from"

# Expected output:
# INFO [ppa.predictor] Loaded model from /models/test-app/ppa_model.tflite, scaler from /models/test-app/scaler.pkl
```

---

## Scaling Behavior

### Monitor Scaling Decisions

```bash
# Watch deployment replicas change
kubectl get deploy test-app -w

# View scaling history from CR status
kubectl get ppa test-app-ppa -o jsonpath='{.status}' | jq .

# Example:
# {
#   "lastPredictedLoad": 287.3,
#   "currentReplicas": 4,
#   "desiredReplicas": 4,
#   "lastScaleTime": "2026-03-09T14:32:15.123456Z",
#   "consecutiveSkips": 0
# }
```

### Rate Limiting

The operator applies rate limits to prevent thrashing:

```yaml
scaleUpRate: 2.0        # Max 2× current replicas per cycle (30s)
scaleDownRate: 0.5      # Max 50% reduction per cycle (30s)
minReplicas: 2          # Hard floor
maxReplicas: 20         # Hard ceiling
```

**Example:**
```
Current: 4 replicas
Predicted load: 100 req/s
Capacity per pod: 50 req/s
Raw desired = ceil(100/50) = 2
Max up = ceil(4 × 2.0) = 8
Min down = floor(4 × 0.5) = 2
Actual desired = max(2, min(8, 2)) = 2

→ No scaling (2 already at min, within rate limits)
```

---

## Error Handling & Troubleshooting

### Prometheus Unavailable

```
Symptom: consecutiveSkips keeps incrementing, no scaling
```

```bash
# Check Prometheus health
kubectl get pods -n monitoring | grep prometheus

# Check operator logs
kubectl logs -l app=ppa-operator | grep "Prometheus"

# Expected output after 10 failures:
# ERROR [ppa.features] Prometheus query failed (10 consecutive): ...
# CR status.consecutiveSkips = 10
```

**Resolution:**
```bash
# Restart Prometheus stack
helm upgrade prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring

# Wait for Prometheus to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=prometheus -n monitoring --timeout=300s

# Verify operator recovers (logs should show successful queries)
```

### Model Loading Failed

```
Symptom: Operator logs show "Failed to load model" and predictions always return 0
```

```bash
# Check model file exists on PVC
kubectl exec -it deployment/ppa-operator -- ls -la /models/test-app/

# Check file permissions
kubectl exec -it deployment/ppa-operator -- file /models/test-app/ppa_model.tflite

# Check TFLite runtime is installed
kubectl exec -it deployment/ppa-operator -- python -c "import tflite_runtime; print('OK')"

# Check operator logs for specifics
kubectl logs -l app=ppa-operator | grep "ppa.predictor"
```

**Resolution:**
```bash
# Rebuild operator image with tflite_runtime installed
docker build -t ppa-operator:latest operator/

# Redeploy
kubectl rollout restart deployment/ppa-operator

# Push new model and trigger reload via CR patch
```

### Window Warmup Delay

```
Symptom: No scaling for first 6-7 minutes (no logs about "Predicted load")
```

This is **normal** — the operator is collecting feature samples:
```
LOOKBACK_STEPS = 12
TIMER_INTERVAL = 30 seconds
Warmup = 12 × 30s = 360s = 6 minutes
```

```bash
# Watch warmup progress
kubectl logs -l app=ppa-operator -f | grep "Warming up"

# Expected output:
# INFO [ppa.operator] [test-app-ppa] Warming up: 1/12 steps collected
# INFO [ppa.operator] [test-app-ppa] Warming up: 2/12 steps collected
# ...
# INFO [ppa.operator] [test-app-ppa] Warming up: 12/12 steps collected
# INFO [ppa.operator] [test-app-ppa] Predicted load: 287.3 req/s
```

### No Scaling Despite Ready Model

```
Symptom: Operator is ready and predicting but replicas don't change
```

**Check stabilization filter:**
```bash
# Operator requires 2 consecutive predictions with <10% change
# If underlying load fluctuates wildly, stabilization will hold

# Check CR logs
kubectl logs -l app=ppa-operator | grep "Stabilizing"

# Expected output:
# INFO [ppa.operator] [test-app-ppa] Stabilizing: 0/2 stable reads
# INFO [ppa.operator] [test-app-ppa] Stabilizing: 1/2 stable reads
# INFO [ppa.operator] [test-app-ppa] No scaling needed: 4 replicas is correct
```

**To reduce stabilization:**
```yaml
# Edit CR
kubectl patch ppa test-app-ppa --type merge \
  -p '{"spec":{"stabilizationSteps":1}}'

# Or env var in deployment
PPA_STABILIZATION_STEPS: "1"
```

---

## Health Probes

### Liveness Probe (Restart on Hang)

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3
```

**Behavior:** If 3 consecutive /healthz requests fail, K8s kills and restarts the pod.

### Readiness Probe (Service Endpoint)

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```

**Behavior:** If /healthz fails, pod is removed from service endpoints.

### Manual Health Check

```bash
# Port-forward to operator pod
kubectl port-forward deployment/ppa-operator 8080:8080 &

# Test health endpoint
curl -i http://localhost:8080/healthz
# Expected: HTTP/1.1 200 OK
# Body: ok
```

---

## Updating Operator Code

### Rebuild & Redeploy

```bash
# Edit code (operator/main.py, etc.)
vim operator/main.py

# Rebuild image
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f operator/Dockerfile .

# Redeploy (rolling update)
kubectl rollout restart deployment/ppa-operator

# Watch rollout
kubectl rollout status deployment/ppa-operator

# Verify logs from new pod
kubectl logs -l app=ppa-operator -f
```

---

## Multi-CR Setup

The same operator pod can manage multiple PredictiveAutoscaler CRs for different apps:

```bash
# Create second CR for a different app
kubectl apply -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: other-app-ppa
  namespace: default
spec:
  targetDeployment: other-app
  namespace: default
  minReplicas: 2
  maxReplicas: 20
  capacityPerPod: 80
  modelPath: /models/other-app/ppa_model.tflite
  scalerPath: /models/other-app/scaler.pkl
  targetScalerPath: /models/other-app/target_scaler.pkl
EOF

# Operator automatically manages both CRs independently
kubectl get ppa
# NAME                TARGET          NAMESPACE  CURRENT  DESIRED  PREDICTED
# test-app-ppa        test-app        default    4        4        287.3
# other-app-ppa       other-app       default    3        3        156.2

# Scale models on PVC
# /models/test-app/ppa_model.tflite
# /models/other-app/ppa_model.tflite
```

---

## Cleanup & Troubleshooting

### Delete Operator Deployment

```bash
# Keep data
kubectl delete deployment ppa-operator
kubectl delete pvc ppa-models

# Full cleanup
kubectl delete -f deploy/operator-deployment.yaml
kubectl delete -f deploy/predictiveautoscaler.yaml
kubectl delete -f deploy/rbac.yaml
kubectl delete -f deploy/crd.yaml
```

### Reset to Factory State

```bash
# Delete all PPA resources
kubectl delete ppa --all
kubectl delete crd predictiveautoscalers.ppa.example.com
kubectl delete sa ppa-operator
kubectl delete clusterrole ppa-operator
kubectl delete clusterrolebinding ppa-operator

# Redeploy from scratch
./scripts/deploy_operator.sh --horizon rps_t10m
```

---

## See Also

- [Operator Architecture](../architecture/ml_operator.md) — Internals & design
- [ML Commands Reference](./ml_commands.md) — Model training & promotion
- [PPA Commands Reference](./ppa_commands.md) — Cluster setup & infrastructure
