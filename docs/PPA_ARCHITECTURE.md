---
title: PPA Architecture
description: Detailed internals of the Predictive Pod Autoscaler
last-updated: 2026-05-16
---

# PPA Architecture {#ppa-architecture}

## Core Control Loop {#control-loop}

**Source:** `src/ppa/operator/main.py:1448`

```python
@kopf.timer("ppa.example.com", "v1", "predictiveautoscalers", interval=30)
def reconcile(spec, status, meta, patch, **kwargs):
    """Main reconciliation loop - runs every 30 seconds per CR."""
```

### Step 1: Parse CRD Spec {#step-1-parse-crd}

**Source:** `src/ppa/operator/main.py:666`

```python
def _parse_crd_spec(spec, status, meta, cr_ns, cr_name, patch):
    """Parse CR spec, resolve model paths, manage CR state."""
```

**Resolved configuration:**
| Field | Default | Source |
|-------|---------|--------|
| `targetDeployment` | required | CR spec |
| `namespace` | CR namespace | CR spec |
| `appName` | targetDeployment | CR spec |
| `horizon` | `rps_t10m` | CR spec |
| `minReplicas` | 2 | `DEFAULT_MIN_REPLICAS` |
| `maxReplicas` | 20 | CR spec |
| `capacityPerPod` | 50 | `DEFAULT_CAPACITY_PER_POD` |
| `scaleUpRate` | 2.0 | `DEFAULT_SCALE_UP_RATE` |
| `scaleDownRate` | 0.5 | `DEFAULT_SCALE_DOWN_RATE` |
| `safetyFactor` | 1.10 | `spec.safetyFactor` |
| `observerMode` | false | CR spec |
| `prometheusUrl` | env default | CR spec or env |

**See:** [[CLI_REFERENCE.md#predictiveautoscaler-spec]]

### Step 2: Fetch Features {#step-2-fetch-features}

**Source:** `src/ppa/operator/main.py:1013` | `src/ppa/operator/features.py`

```python
def build_feature_vector(target, target_ns, min_r, max_r, container_name, prom_url, state):
    """Extract 14 features from Prometheus metrics."""
```

**Feature vector** (`FEATURE_COLUMNS` from `src/ppa/config.py:18-39`):

| Feature | Type | PromQL Query |
|---------|------|--------------|
| `rps_per_replica` | Queried | `sum(rate(requests_total[5m])) / replicas` |
| `cpu_utilization_pct` | Queried | `container_cpu_usage / limit` |
| `memory_utilization_pct` | Queried | `container_memory_usage / limit` |
| `latency_p95_ms` | Queried | `histogram_quantile(0.95, latency_bucket)` |
| `active_connections` | Queried | `sum(active_connections)` |
| `error_rate` | Queried | `rate(errors_total[5m]) / rate(requests_total[5m])` |
| `cpu_acceleration` | Derived | `d(cpu_utilization_pct)/dt` |
| `rps_acceleration` | Derived | `d(rps_per_replica)/dt` |
| `replicas_normalized` | Derived | `current_replicas / max_replicas` |
| `hour_sin`, `hour_cos` | Temporal | Time encoding |
| `dow_sin`, `dow_cos` | Temporal | Day of week |
| `is_weekend` | Temporal | Weekend flag |

### Step 3: Update Predictor {#step-3-update-predictor}

**Source:** `src/ppa/operator/main.py:1076` | `src/ppa/operator/predictor.py`

```python
def _update_predictor_state(cr_name, cr_ns, config, state, features, current, patch):
    """Update predictor history and check readiness."""
```

**Warmup period:** Requires 60 steps (30 min at 30s interval) before predictions are valid.

```python
# src/ppa/operator/main.py:1100-1120
if not state.predictor.ready():
    logger.info(f"Warming up: {history_len}/{maxlen} steps collected")
    return False  # Skip prediction until warm
```

### Step 4: Make Scaling Decision {#step-4-scaling-decision}

**Source:** `src/ppa/operator/main.py:1145`

```python
def _make_scaling_decision(cr_name, cr_ns, config, state, features, raw_metrics, current, patch):
    """Predict load, check drift, stabilize, calculate target replicas."""
```

**Algorithm:**
1. **Predict:** `predicted_load = predictor.predict()` (LSTM inference)
2. **Apply safety factor:** `inflated = predicted_load * 1.10`
3. **Calculate raw target:** `raw_desired = ceil(inflated / capacity_per_pod)`
4. **Rate limit:** Apply scale up/down rate constraints
5. **Stabilize:** Require 2 consecutive cycles within ±0.5 replicas

**Multi-model aggregation** (lines 1286-1338):
- Collect predictions from all models targeting same deployment
- Discard stale predictions (older than 2× timer interval)
- If 2+ models disagree by 5x ratio, skip scaling
- Otherwise use **median** of valid predictions

### Step 5: Apply Scaling {#step-5-apply-scaling}

**Source:** `src/ppa/operator/main.py:1381` | `src/ppa/operator/scaler.py`

```python
def _apply_scaling(cr_name, cr_ns, config, state, desired, current, patch):
    """Apply scaling decision to deployment."""
```

**Guards:**
- **Observer mode:** Only log, don't scale
- **Scale backoff:** Exponential backoff on failures (30s → 300s max)
- **Rate limiting:** Max change per cycle: ×2.0 up, ×0.5 down

---

## ML Pipeline {#ml-pipeline}

### Model Training {#model-training}

**Source:** `src/ppa/model/`

```
Historical Data → Feature Extraction → Train LSTM → Convert to TFLite → Upload
```

**Training pipeline:**
1. Extract 60-step sliding windows from Prometheus
2. Train LSTM with 10-minute lookahead target
3. Convert to TensorFlow Lite (`.tflite`)
4. Upload to model registry

### Inference {#inference}

**Source:** `src/ppa/operator/predictor.py:Predictor`

```python
class Predictor:
    def __init__(self, model_path, scaler_path, target_scaler_path=None, metadata_path=None, version=""):
        # Load TFLite interpreter
        # Load sklearn StandardScaler
        self.interpreter = tf.lite.Interpreter(model_path=model_path)
        self.scaler = pickle.load(open(scaler_path, 'rb'))
        
    def predict(self):
        features = self.history.get_latest()  # 14 features
        scaled = self.scaler.transform([features])
        self.interpreter.set_tensor(input_index, scaled)
        self.interpreter.invoke()
        prediction = self.interpreter.get_tensor(output_index)
        return prediction
```

**Inference latency:** <100ms (TFLite on CPU)

---

## Prometheus Metrics {#prometheus-metrics}

**Source:** `src/ppa/operator/main.py:50-80`

| Metric | Type | Description |
|--------|------|-------------|
| `ppa_predicted_load_rps` | Gauge | LSTM predicted load |
| `ppa_inflated_load_rps` | Gauge | Predicted × safety factor |
| `ppa_raw_desired_replicas` | Gauge | Pre-rate-limit target |
| `ppa_desired_replicas` | Gauge | Final target replicas |
| `ppa_current_replicas` | Gauge | Observed ready replicas |
| `ppa_scale_events_total` | Counter | Scaling events |
| `ppa_circuit_breaker_tripped` | Gauge | Metric circuit breaker |
| `ppa_concept_drift_detected` | Gauge | Drift detection flag |
| `ppa_prediction_error_pct` | Gauge | MAPE % |
| `ppa_inference_latency_ms` | Gauge | Model inference time |

Metrics endpoint: `:9100/metrics`

---

## Configuration {#configuration}

### Environment Variables {#env-vars}

| Variable | Default | Description |
|----------|---------|-------------|
| `PPA_MODEL_DIR` | `/models` | Model artifact directory |
| `PPA_PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus endpoint |
| `PPA_NAMESPACE` | `default` | Default namespace |
| `PPA_TIMER_INTERVAL` | 30 | Reconciliation interval (seconds) |
| `PPA_INITIAL_DELAY` | 60 | Startup delay |
| `PPA_STABILIZATION_STEPS` | 2 | Cycles to stabilize |
| `PPA_STABILIZATION_TOLERANCE` | 0.5 | Replica tolerance |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

### Feature Configuration {#feature-config}

**Source:** `src/ppa/config.py:18-41`

```python
QUERIED_FEATURES = [
    "rps_per_replica",
    "cpu_utilization_pct", 
    "memory_utilization_pct",
    "latency_p95_ms",
    "active_connections",
    "error_rate",
    "cpu_acceleration",
    "rps_acceleration",
    "replicas_normalized",
]

TEMPORAL_FEATURES = [
    "hour_sin", "hour_cos",
    "dow_sin", "dow_cos",
    "is_weekend",
]

FEATURE_COLUMNS = QUERIED_FEATURES + TEMPORAL_FEATURES
```

---

## Error Handling {#error-handling}

### Circuit Breaker {#circuit-breaker}

**Source:** `src/ppa/operator/main.py:1056-1070`

Trips after 10 consecutive Prometheus metric failures.
- Stops scaling decisions
- Preserves current replica count
- Manual recovery or auto-recovery after metric health

### Scale Backoff {#backoff}

**Source:** `src/ppa/operator/main.py:220-222` | `1404-1423`

Exponential backoff on scaling failures:
- Base: 30 seconds
- Max: 300 seconds
- Jitter: ±20%
- Reset on success

### Concept Drift {#concept-drift}

**Source:** `src/ppa/operator/predictor.py:check_concept_drift()`

Detects when prediction error exceeds threshold:
- Threshold: 20% MAPE
- Severity: warning / critical
- Action: Log retranning trigger recommendation

---

## See Also {#see-also}

- [Architecture - PPA Data Flow](ARCHITECTURE.md#ppa-data-flow) - High-level data flow
- [Components - Operator](COMPONENTS.md#operator) - Operator API reference
- [Operations - Model Training](OPERATIONS.md#model-training) - Model training guide
- [CLI Reference - PPA Commands](CLI_REFERENCE.md#ppa-commands) - CLI commands
