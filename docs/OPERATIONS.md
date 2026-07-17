---
title: Operations Guide
description: Installation, configuration, troubleshooting, and monitoring
last-updated: 2026-05-16
---

# Operations Guide {#operations}

## Prerequisites {#prerequisites}

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Kubernetes | 1.25+ | Cluster for running operator |
| Prometheus | 2.35+ | Metrics collection |
| Python | 3.9+ | CLI and tooling |
| Docker | 20+ | Container builds |
| kubectl | Latest | Cluster access |

---

## Installation {#installation}

### Step 1: Install PPA {#install-ppa}

```bash
# Clone repository
git clone https://github.com/your-org/predictive-pod-autoscaler.git
cd predictive-pod-autoscaler

# Install Python dependencies
pip install -e ".[dev]"

# Verify installation
ppa --version
```

### Step 2: Deploy Operator {#deploy-operator}

```bash
# Build operator image
ppa operator build

# Deploy to cluster
ppa operator deploy

# Check status
ppa operator status

# View logs
ppa operator logs
```

### Step 3: Configure Prometheus {#configure-prometheus}

Ensure Prometheus can scrape your target applications. Applications must expose:
- `/metrics` endpoint (Prometheus format)
- CPU and memory limits configured

---

## Configuration {#configuration}

### Environment Variables {#env-vars}

| Variable | Default | Description |
|----------|---------|-------------|
| `PPA_MODEL_DIR` | `/models` | Directory containing `.tflite` models |
| `PPA_PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus endpoint |
| `PPA_NAMESPACE` | `default` | Default Kubernetes namespace |
| `PPA_TIMER_INTERVAL` | `30` | Reconciliation interval (seconds) |
| `PPA_INITIAL_DELAY` | `60` | Initial delay before first reconciliation |
| `PPA_STABILIZATION_STEPS` | `2` | Cycles to stabilize before scaling |
| `PPA_STABILIZATION_TOLERANCE` | `0.5` | Replica tolerance for stabilization |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

### CRD Specification {#crd-spec}

**Source:** `deploy/predictiveautoscaler.yaml`

```yaml
apiVersion: autoscaling.ppa.io/v1alpha1
kind: PredictiveAutoscaler
metadata:
  name: my-app-ppa        # Required: Unique name
  namespace: default      # Required: Namespace
spec:
  targetRef:
    apiGroup: apps        # Required: Target API group
    kind: Deployment      # Required: Target kind
    name: my-app          # Required: Target name
  modelId: lstm-rps-v2    # Required: Model identifier
  horizon: rps_t10m       # Prediction horizon (default: rps_t10m)
  minReplicas: 2          # Minimum replicas
  maxReplicas: 50         # Maximum replicas
  capacityPerPod: 50      # Capacity per pod (req/s)
  scaleUpRate: 2.0        # Scale up multiplier per cycle
  scaleDownRate: 0.5      # Scale down multiplier per cycle
  safetyFactor: 1.10      # Safety factor (1.10 = 10%)
  observerMode: false     # true = observe only, no scaling
  prometheusUrl: ""       # Optional: Override Prometheus URL
  containerName: ""       # Optional: Container name for metrics
```

### Model Directory Structure {#model-structure}

```
/models/
├── {app-name}/           # Application name
│   ├── rps_t3m/          # 3-minute horizon
│   │   └── current/      # Active model symlink
│   │       ├── ppa_model.tflite
│   │       ├── scaler.pkl
│   │       └── target_scaler.pkl (optional)
│   └── rps_t10m/         # 10-minute horizon
│       └── current/
│           ├── ppa_model.tflite
│           └── scaler.pkl
```

---

## Application Onboarding {#application-onboarding}

### Step 1: Annotate Application {#annotate-app}

Add these annotations to your Deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/path: "/metrics"
    prometheus.io/port: "8080"
spec:
  template:
    spec:
      containers:
      - name: my-app
        resources:
          requests:
            cpu: "100m"
            memory: "128Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
```

### Step 2: Create PredictiveAutoscaler {#create-autoscaler}

```bash
# Create CR
cat <<EOF | kubectl apply -f -
apiVersion: autoscaling.ppa.io/v1alpha1
kind: PredictiveAutoscaler
metadata:
  name: my-app-ppa
  namespace: default
spec:
  targetRef:
    apiGroup: apps
    kind: Deployment
    name: my-app
  modelId: lstm-rps-v2
  horizon: rps_t10m
  minReplicas: 2
  maxReplicas: 10
  capacityPerPod: 50
  safetyFactor: 1.10
  observerMode: false
EOF
```

### Step 3: Verify {#verify-autoscaler}

```bash
# Check CR status
kubectl get predictiveautoscaler my-app-ppa -o yaml

# View prediction metrics
kubectl port-forward svc/prometheus 9090:9090
curl http://localhost:9090/api/v1/query?query=ppa_desired_replicas

# Check operator logs
kubectl logs -l app=ppa-operator -f
```

---

## Troubleshooting {#troubleshooting}

### Common Issues {#common-issues}

| Issue | Symptoms | Resolution |
|-------|----------|------------|
| Model not found | `ARTIFACT LOAD FAILED` | Check `/models/{app}/{horizon}/current/` symlink exists |
| Prometheus unreachable | `circuit breaker tripped` | Verify `PPA_PROMETHEUS_URL` and network connectivity |
| Warmup period too long | 30 min before scaling | History window requires 60 steps (30 min at 30s interval) |
| Scale oscillation | Frequent up/down scaling | Increase `stabilizationSteps` or adjust `safetyFactor` |
| Prediction error high | `concept drift detected` | Retrain in the centralized model plane, then deliver the new bundle with `ppa model push` |

### Diagnostic Commands {#diagnostics}

```bash
# Check model artifacts
ls -la /models/{app}/{horizon}/current/

# Test Prometheus connectivity
curl -s http://prometheus:9090/api/v1/query?query=up

# Check operator health
curl http://<operator-pod>:8080/healthz

# View Prometheus metrics
curl http://<operator-pod>:9100/metrics

# Check CR status
kubectl get predictiveautoscaler -o yaml
```

### Logging {#logging}

```bash
# Operator logs
kubectl logs -l app=ppa-operator -f

# Increase log verbosity
LOG_LEVEL=DEBUG
```

---

## Monitoring {#monitoring}

### Prometheus Metrics {#prometheus-metrics}

| Metric | Alert Threshold | Description |
|--------|-----------------|-------------|
| `ppa_predicted_load_rps` | — | Predicted load (monitor for anomalies) |
| `ppa_desired_replicas` | — | Target replicas |
| `ppa_scale_events_total` | Rate > 5/5m | Too frequent scaling |
| `ppa_circuit_breaker_tripped` | 1 | Circuit breaker active |
| `ppa_concept_drift_detected` | 1 | Drift detected |
| `ppa_prediction_error_pct` | > 20% | High error rate |

### Health Endpoints {#health}

| Endpoint | Port | Description |
|----------|------|-------------|
| `/healthz` | 8080 | Liveness probe |
| `/metrics` | 9100 | Prometheus scraping |

### Dashboards {#dashboards}

Grafana dashboard ID: `predictive-pod-autoscaler`

Panels:
- Predicted vs Actual RPS
- Replica count over time
- Prediction error %
- Scale events timeline

---

## Model Training {#model-training}

### Train New Model {#train}

```bash
# Collect training data
ppa data collect --app my-app --duration 24h

# Train model
ppa model train --app my-app --horizon rps_t10m

# Upload to model registry
ppa model upload --app my-app --horizon rps_t10m --path /models/my-app/rps_t10m/v1
```

### Model Validation {#validate}

```bash
# Test model performance
ppa model test --app my-app --horizon rps_t10m

# Compare with champion
ppa model compare --champion v1 --challenger v2
```

---

## Backup and Recovery {#backup-recovery}

### Backup {#backup-procedure}

```bash
# Backup SQLite audit database
sqlite3 data/nexus.db ".backup audit_backup.db"

# Export model artifacts
tar -czf models_backup.tar.gz /models/
```

### Recovery {#recovery}

```bash
# Restore model artifacts
tar -xzf models_backup.tar.gz -C /models/

# Restore audit database
sqlite3 data/nexus.db ".restore audit_backup.db"
```

---

## See Also {#see-also}

- [Architecture](ARCHITECTURE.md) - System architecture
- [CLI Reference](CLI_REFERENCE.md) - CLI command reference
- [Developer Guide](DEVELOPER.md) - Developer guide
- [Components](COMPONENTS.md) - Component reference
