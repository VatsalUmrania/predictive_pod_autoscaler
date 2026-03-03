# PLAN — PPA Phase 2: LSTM Model + Embedded Operator

**Predictive Pod Autoscaler | Semester 6 | March 2026**

---

## Goal

Build the LSTM prediction model and embedded kopf operator. Single pod reads Prometheus, runs TF Lite inference, scales via K8s API. No Flask, no KEDA, no sidecars.

## Architecture

```
┌─────────────────────────────────────────────┐
│           kopf Operator (single pod)         │
│                                              │
│  Prometheus ──► Feature Builder ──► TF Lite  │
│  Client            (pandas)        Model     │
│                                       │      │
│                              Replica Calc    │
│                                       │      │
│                          kubernetes-client   │
│                                       │      │
└───────────────────────────────────────┼──────┘
                                        ▼
                              K8s API Server
                           (scale deployment)
```

---

## Final 9-Feature Vector

| # | Feature | Source | PromQL |
|---|---|---|---|
| 1 | `requests_per_second` | app.py | `sum(rate(http_requests_total{pod=~"test-app.*"}[1m]))` |
| 2 | `latency_p95_ms` | app.py | `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{pod=~"test-app.*"}[5m])) by (le)) * 1000` |
| 3 | `cpu_usage_percent` | cAdvisor | `sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m]))*100` |
| 4 | `memory_usage_bytes` | cAdvisor | `sum(container_memory_working_set_bytes{pod=~"test-app.*"})` |
| 5 | `hour_sin` | Generated | `sin(2π × hour / 24)` |
| 6 | `hour_cos` | Generated | `cos(2π × hour / 24)` |
| 7 | `dow_sin` | Generated | `sin(2π × dow / 7)` |
| 8 | `dow_cos` | Generated | `cos(2π × dow / 7)` |
| 9 | `current_replicas` | kube-state-metrics | `kube_deployment_status_replicas{deployment="test-app"}` |

**Order matters**: primary signal first, state context last.

---

## Operator Config

- **Timer interval**: 30s (matches scrape cycle, avoids stale reads)
- **Initial delay**: 60s (wait for metrics warmup)
- **Stabilization**: 2 consecutive stable reads before scaling
- **Scale-up rate limit**: 2× current replicas per cycle
- **Scale-down rate**: 50% reduction per cycle
- **Min/Max replicas**: 2–20

---

## Project Structure

```
predictive_pod_autoscaler/
├── data-collection/               # existing
├── model/
│   ├── train.py                   # CSV → Keras LSTM
│   ├── convert.py                 # Keras → .tflite + quantize
│   ├── evaluate.py                # MAPE, MAE, plots, HPA comparison
│   └── artifacts/                 # gitignored
├── operator/
│   ├── main.py                    # kopf @timer — thin orchestrator
│   ├── features.py                # Prometheus → DataFrame
│   ├── predictor.py               # TFLite wrapper
│   ├── scaler.py                  # Replica calc + k8s client
│   ├── config.py                  # All constants
│   └── Dockerfile
├── deploy/
│   ├── crd.yaml                   # PredictiveAutoscaler CRD
│   ├── rbac.yaml                  # SA + ClusterRole + Binding
│   ├── operator-deployment.yaml
│   └── predictiveautoscaler.yaml  # Example CR
├── tests/
│   ├── locustfile.py              # Variable traffic pattern
│   ├── test_predictor.py
│   └── test_scaler.py
└── notebooks/                     # Exploratory (not deployed)
    ├── 01_eda.ipynb
    ├── 02_feature_eng.ipynb
    └── 03_lstm_train.ipynb
```

---

## Phase Breakdown

### Phase 2A — Update Data Pipeline (immediate)

| Task | File | Change |
|---|---|---|
| Update feature queries | `export_training_data.py` | 9-feature vector, cyclical encoding |
| Update verification | `verify_features.py` | Match new features |
| Update query docs | `docs/reference/working_queries.md` | New table |
| Variable traffic | `locustfile.py` | Sine-wave pattern for LSTM training signal |

### Phase 2B — Scaffold Project Structure

Create all directories and skeleton files for `model/`, `operator/`, `deploy/`, `notebooks/`.

### Phase 2C — LSTM Model Training

| Task | File | Details |
|---|---|---|
| Training pipeline | `model/train.py` | Load CSV → normalize → LSTM → save `.keras` |
| TFLite conversion | `model/convert.py` | `.keras` → `.tflite` + int8 quantize |
| Evaluation | `model/evaluate.py` | MAPE, MAE, plot predicted vs actual, compare vs HPA |

**Prerequisite**: ≥5,000 rows in CSV, <5% nulls, variance in `requests_per_second`.

### Phase 2D — kopf Operator

| Task | File | Details |
|---|---|---|
| Prometheus client | `operator/features.py` | Fetch 9 features, build DataFrame |
| TFLite wrapper | `operator/predictor.py` | Load model, preprocess, infer |
| Replica calculator | `operator/scaler.py` | predicted_load → desired_replicas, rate limiting |
| Timer handler | `operator/main.py` | kopf @timer(30s), stabilization window |

### Phase 2E — K8s Deployment

| Task | File | Details |
|---|---|---|
| CRD | `deploy/crd.yaml` | `PredictiveAutoscaler` custom resource |
| RBAC | `deploy/rbac.yaml` | SA + ClusterRole (deployments/scale, pods, CRs) |
| Operator deployment | `deploy/operator-deployment.yaml` | Single pod with model artifact |
| Example CR | `deploy/predictiveautoscaler.yaml` | CR targeting test-app |

### Phase 2F — Testing

| Task | File | Details |
|---|---|---|
| Unit: predictor | `tests/test_predictor.py` | TFLite inference with known input |
| Unit: scaler | `tests/test_scaler.py` | Replica calc edge cases |
| Load: Locust | `tests/locustfile.py` | Variable traffic ramp |

---

## Verification Checklist

- [ ] CSV has >5,000 rows with variance
- [ ] LSTM MAPE < 15% on test set
- [ ] Operator scales up within 30–60s of load increase
- [ ] Operator scales down within 2–5 min of load decrease
- [ ] PPA reacts faster than HPA (side-by-side comparison)
- [ ] CRD installs cleanly on Minikube
- [ ] RBAC allows operator to patch deployments/scale
- [ ] No crash loops after 24h of continuous operation

---

## What to Build First

1. Update data pipeline (9-feature vector)
2. Deploy variable traffic Locust pattern
3. Collect 3–7 days of data
4. Train LSTM in notebooks
5. Productionize in `model/train.py`
6. Build operator
