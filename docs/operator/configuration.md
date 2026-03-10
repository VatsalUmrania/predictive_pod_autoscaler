# Operator Configuration Reference

**Environment variables, Custom Resource specification, and tuning parameters**

---

## Environment Variables

The operator reads these environment variables to configure behavior:

### Timing & Reconciliation

| Variable | Default | Range | Description |
|---|---|---|---|
| `PPA_TIMER_INTERVAL` | `30` | 10–300 | Reconciliation cycle interval (seconds) |
| `PPA_LOOKBACK_STEPS` | `12` | 3–60 | Number of 30s steps to collect (window = steps × 30s) |
| `PPA_STABILIZATION_STEPS` | `2` | 1–10 | Consecutive cycles required before scaling change |

### Prometheus

| Variable | Default | Description |
|---|---|---|
| `PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus server address |
| `PROMETHEUS_TIMEOUT` | `10` | Query timeout (seconds) |

### Logging

| Variable | Default | Options | Description |
|---|---|---|---|
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR | Verbosity of operator logs |
| `TF_CPP_MIN_LOG_LEVEL` | `2` | 0–3 | TensorFlow logging (0=verbose, 3=errors only) |

### Example: Aggressive Tuning

```bash
# Faster response time, more aggressive scaling
PPA_TIMER_INTERVAL=15         # Reconcile every 15s instead of 30s
PPA_LOOKBACK_STEPS=8          # Use 4-min window instead of 6-min
PPA_STABILIZATION_STEPS=1     # Scale immediately (no stabilization)

# Set in deployment:
kubectl set env deployment/ppa-operator \
  PPA_TIMER_INTERVAL=15 \
  PPA_LOOKBACK_STEPS=8 \
  PPA_STABILIZATION_STEPS=1
```

---

## Custom Resource (CR) Specification

### Full Schema

```yaml
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: <app-name>-ppa              # CR name (unique per namespace)
  namespace: default                 # Kubernetes namespace
spec:
  # Target Deployment
  targetDeployment: <deployment-name>
  namespace: default                 # Deployment's namespace
  containerName: ""                  # (optional) specific container to target

  # Model & Scaler Paths (in /models PVC)
  modelPath: /models/<app>/ppa_model.tflite
  scalerPath: /models/<app>/scaler.pkl
  targetScalerPath: /models/<app>/target_scaler.pkl

  # Scaling Bounds
  minReplicas: 2                     # Minimum pod count
  maxReplicas: 20                    # Maximum pod count
  capacityPerPod: 80                 # RPS per pod at full capacity

  # Rate Limits (prevent thrashing)
  scaleUpRate: 2.0                   # Max multiplier per cycle (e.g., 5 → 10)
  scaleDownRate: 0.5                 # Min multiplier per cycle (e.g., 10 → 5)

status:
  # (Read-only, updated by operator)
  consecutiveSkips: 0
  lastPrediction:
    timestamp: "2026-03-10T10:00:00Z"
    predictedRPS: 1500
    desiredReplicas: 19
    currentReplicas: 18
    reason: "Scaling up: traffic spike detected"
```

### Field Descriptions

#### Metadata

| Field | Type | Required | Description |
|---|---|---|---|
| `metadata.name` | string | ✓ | Unique CR name (e.g., `test-app-ppa`, `api-server-ppa`) |
| `metadata.namespace` | string | ✓ | Kubernetes namespace (usually `default`) |

#### Spec

##### Target Deployment

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `spec.targetDeployment` | string | ✓ | - | Deployment to autoscale |
| `spec.namespace` | string | ✓ | `default` | Deployment's namespace |
| `spec.containerName` | string | ✗ | Auto-detect | Specific container to target (unnecessary if pod has 1 container) |

##### Model & Scaler

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `spec.modelPath` | string | ✓ | - | Path to `.tflite` model in `/models` PVC |
| `spec.scalerPath` | string | ✓ | - | Path to input `scaler.pkl` (feature normalization) |
| `spec.targetScalerPath` | string | ✓ | - | Path to output `target_scaler.pkl` (RPS denormalization) |

**Example paths:**
```
/models/test-app/ppa_model.tflite
/models/test-app/scaler.pkl
/models/test-app/target_scaler.pkl
```

##### Scaling Bounds

| Field | Type | Required | Default | Range | Description |
|---|---|---|---|---|---|
| `spec.minReplicas` | integer | ✓ | - | 1–X | Minimum replicas (fail-safe lower bound) |
| `spec.maxReplicas` | integer | ✓ | - | X–100+ | Maximum replicas (cost/resource upper bound) |
| `spec.capacityPerPod` | float | ✓ | - | > 0 | RPS/pod at 100% utilization (determines scaling target) |

**Example:**
```yaml
minReplicas: 2              # Never scale below 2 pods
maxReplicas: 50             # Never scale above 50 pods
capacityPerPod: 80          # Assume 80 RPS per pod = target utilization
# If prediction is 1600 RPS → desired = 1600/80 = 20 replicas (between 2–50)
```

##### Rate Limits

| Field | Type | Required | Default | Valid | Description |
|---|---|---|---|---|---|
| `spec.scaleUpRate` | float | ✓ | - | ≥ 1.0 | Max replicas multiplier per cycle (e.g., 2.0 = 2× max jump) |
| `spec.scaleDownRate` | float | ✓ | - | 0.1–1.0 | Min replicas multiplier per cycle (e.g., 0.5 = 50% min floor) |

**Examples:**

| Current | Desired | scaleUpRate=2.0 | scaleDownRate=0.5 | Final |
|---|---|---|---|---|
| 5 | 15 | 5×2=10 ≤ 15 | - | **10** (rate-limited up) |
| 10 | 20 | 10×2=20 ≤ 20 | - | **20** (allowed up) |
| 10 | 3 | - | 10×0.5=5 ≥ 3 | **5** (rate-limited down) |
| 10 | 2 | - | 10×0.5=5 ≥ 2 | **5** (rate-limited down, then enforces minReplicas=2 on next cycle) |

#### Status (Read-Only)

| Field | Type | Description |
|---|---|---|
| `status.consecutiveSkips` | integer | How many cycles the CR was skipped (e.g., due to model load error) |

---

## Tuning Guide

### Conservative (Safe, Slower Response)

**Goal:** Avoid flapping, prefer stability over responsiveness

```yaml
spec:
  scaleUpRate: 1.5              # Slow scale-up
  scaleDownRate: 0.7            # Conservative scale-down
  capacityPerPod: 60            # Low capacity = over-provision
```

**+ Environment:**
```bash
PPA_TIMER_INTERVAL=60           # Reconcile every 60s (vs default 30s)
PPA_LOOKBACK_STEPS=20           # 10-min window (vs default 6-min)
PPA_STABILIZATION_STEPS=3       # Require 3-cycle agreement (vs default 2)
```

### Balanced (Recommended Production)

```yaml
spec:
  scaleUpRate: 2.0              # Standard 2× per cycle
  scaleDownRate: 0.5            # Standard 50% per cycle
  capacityPerPod: 80            # Reasonable capacity per pod
```

**+ Environment:**
```bash
PPA_TIMER_INTERVAL=30           # Standard 30s reconciliation
PPA_LOOKBACK_STEPS=12           # Standard 6-min window
PPA_STABILIZATION_STEPS=2       # Standard 2-cycle confirmation
```

### Aggressive (Fast Response, More Churn)

**Goal:** Minimize latency spike response time, accept more scaling changes

```yaml
spec:
  scaleUpRate: 3.0              # Aggressive scale-up
  scaleDownRate: 0.3            # More aggressive scale-down
  capacityPerPod: 100           # High capacity = less over-provisioning
```

**+ Environment:**
```bash
PPA_TIMER_INTERVAL=15           # Reconcile every 15s
PPA_LOOKBACK_STEPS=8            # 4-min window (shorter, more reactive)
PPA_STABILIZATION_STEPS=1       # Scale immediately on prediction
```

---

## Model-Specific Configuration

Different models require different capacity settings:

### RPS at T+3 minutes (rps_t3m)

**Use for:** Apps where cold start latency is < 3 minutes

```yaml
capacityPerPod: 100             # Higher = scale fewer replicas ahead
scaleUpRate: 2.5                # More aggressive (shorter horizon)
```

### RPS at T+5 minutes (rps_t5m) — **RECOMMENDED**

**Use for:** Most applications (sweet spot)

```yaml
capacityPerPod: 80              # Balanced capacity estimate
scaleUpRate: 2.0                # Standard scaling
```

### RPS at T+10 minutes (rps_t10m)

**Use for:** Apps with longer deployment/boot times

```yaml
capacityPerPod: 60              # Conservative (over-provision)
scaleUpRate: 1.5                # Slower scaling (longer horizon = more time)
```

---

## Scaling Decision Examples

### Example 1: Bursty Traffic

```yaml
# Given:
capacityPerPod: 80
minReplicas: 2
maxReplicas: 30
scaleUpRate: 2.0
scaleDownRate: 0.5
```

| Cycle | Baseline | Traffic | Predicted RPS | Base Replicas | Rate Limited | Applied |
|---|---|---|---|---|---|---|
| T+0 | Normal | Baseline | 800 | ~10 | Stable | 10 |
| T+30s | Spike starts | +50% | 1200 | ~15 | 10×2=20 ✓ | 15 |
| T+60s | Sustained | +50% | 1200 | ~15 | Match cycle 1? No | *Hold* |
| T+90s | Sustained | +50% | 1200 | ~15 | Match cycle 1? Yes | **15 ✓** |
| T+120s | Recovered | -50% | 600 | ~7 | 15×0.5=7.5 ✓ | 7 |

### Example 2: Slow Leak

```yaml
capacityPerPod: 100
scaleDownRate: 0.3              # Conservative scale-down
```

| Cycle | Current | Predicted RPS | Base | Rate Limited | Effect |
|---|---|---|---|---|---|
| T+0 | 10 | 800 | 8 | Hold (stabilization) | No change |
| T+30s | 10 | 800 | 8 | 8 ≠ 8 (persistent) | Disagree—reset |
| T+60s | 10 | 750 | 7 | Different from T+30 | Disagree—reset |
| T+90s | 10 | 750 | 7 | Same as T+60 | Agree: 10×0.3=3 → 7 (clamped) |
| T+120s | 7 | 700 | 7 | Match cycle 90s | **Scale to 7** |

---

## Capacity Estimation

### Steps to estimate `capacityPerPod`

1. **Run load test** to find saturation point:
   ```bash
   # Example: use locust or wrk2
   wrk2 -c 100 -d 5m -R 1000 http://app:8080/
   ```

2. **Find max sustainable RPS** (where latency <2s, no errors):
   ```
   Total RPS: 800
   Pod count: 10
   → capacityPerPod ≈ 800 / 10 = 80 RPS/pod
   ```

3. **Add safety margin** (15–20%):
   ```
   80 * 0.85 = 68 → round to 60–70 for headroom
   ```

4. **Set in CR:**
   ```yaml
   capacityPerPod: 70
   ```

---

## Advanced: Namespaced Monitoring

If using namespace-scoped metrics collection, ensure model training uses same namespace:

```yaml
spec:
  namespace: production           # Deployment's namespace
  targetDeployment: web-app       # In namespace 'production'
  modelPath: /models/production-web-app/ppa_model.tflite  # Namespace-aware path
```

This ensures feature engineering matches training data distribution.

---

## See Also

- **[API Reference](./api.md)** — Full CR schema with examples
- **[Commands](./commands.md)** — Useful kubectl commands for checking config
- **[Troubleshooting](./troubleshooting.md)** — Debug capacity/scaling issues

