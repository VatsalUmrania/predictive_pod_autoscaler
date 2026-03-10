# Predictive Pod Autoscaler — Operator Documentation

**Version:** 2.0 · **Last Updated:** 2026-03-10 · **Status:** Production-Ready

The Operator is the live inference component of PPA. It watches Kubernetes Custom Resources (CRs), collects real-time metrics from Prometheus, runs TFLite ML models, and automatically scales your deployments based on 3–10 minute RPS forecasts.

---

## Quick Start

### 1. Prerequisites
- Kubernetes cluster (1.24+) with PVC support
- Prometheus (15s scrape interval)
- Trained ML models in `/models/{app-name}/` PVC

### 2. Deploy Operator

```bash
# 1. Copy trained models to PVC
kubectl cp model/champions/rps_t5m/ppa_model.tflite \
  deployment/ppa-operator:/models/test-app/ --container=operator

# 2. Download operator deployment manifests
kubectl apply -f deploy/crd.yaml              # Defines PredictiveAutoscaler CR
kubectl apply -f deploy/rbac.yaml             # Service account & roles
kubectl apply -f deploy/operator-deployment.yaml    # Operator pod

# 3. Create Custom Resource for your app
kubectl apply -f deploy/predictiveautoscaler.yaml
```

### 3. Monitor

```bash
# Watch operator logs
kubectl logs -f deployment/ppa-operator

# Check CR status
kubectl get ppa -w

# Inspect predictions
kubectl get ppa test-app-ppa -o yaml
```

---

## Documentation Structure

This folder contains comprehensive operator documentation:

| Document | Purpose |
|---|---|
| **[Architecture](./architecture.md)** | System design, component interactions, reconciliation cycle, decision flow |
| **[Deployment](./deployment.md)** | Step-by-step deployment guide, PVC setup, model copying, CRD creation |
| **[Configuration](./configuration.md)** | Environment variables, CR spec, reconciliation timer, rate limits |
| **[API Reference](./api.md)** | Custom Resource schema, field descriptions, validation rules |
| **[Commands](./commands.md)** | Useful kubectl commands for monitoring, debugging, and operations |
| **[Troubleshooting](./troubleshooting.md)** | Common issues, error messages, diagnostic steps |

---

## Key Concepts

### Custom Resource (CR)
A Kubernetes object that tells the operator:
- Which deployment to autoscale
- Which ML model to use
- Scaling bounds (min/max replicas)
- Rate limits (how fast to scale up/down)

**Example:**
```yaml
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: test-app-ppa
  namespace: default
spec:
  targetDeployment: test-app          # Kubernetes Deployment to scale
  modelPath: /models/test-app/ppa_model.tflite
  scalerPath: /models/test-app/scaler.pkl
  minReplicas: 2
  maxReplicas: 20
  capacityPerPod: 80                  # RPS/pod at full capacity
  scaleUpRate: 2.0                    # Max 2× replicas per cycle
  scaleDownRate: 0.5                  # Min 50% replicas per cycle (conservative)
```

### Reconciliation Cycle (30 seconds)
Every 30 seconds, the operator:

1. **Fetches Metrics** — Query Prometheus for last 6 minutes of metrics (12 × 30s)
2. **Builds Feature Window** — Normalize and prepare for model input
3. **Runs Inference** — TFLite model predicts RPS at +5 minutes (or +3m / +10m)
4. **Calculates Replicas** — `desired_replicas = predicted_rps / capacity_per_pod`
5. **Applies Rate Limits** — Ensure replicas don't jump too fast
6. **Stabilization** — Require 2 consecutive cycles of agreement before scaling
7. **Patches Deployment** — `kubectl patch` to update `spec.replicas`
8. **Records Status** — Update CR status with metrics, predictions, decision

---

## Architecture Overview

The operator has several key components working together:

```
┌──────────────────────────────────────────────────────────────┐
│ Operator Pod (ppa-operator Deployment)                       │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  main.py (Kopf Controller)                                  │
│  ├─ @kopf.timer() every 30s                                 │
│  ├─ watches PredictiveAutoscaler CRs                        │
│  └─ calls reconcile() for each CR                           │
│                                                              │
│  features.py                                                │
│  ├─ buildFeatureWindow() — 12-step Prometheus query         │
│  └─ normalizeFeatures() — per-CR scaler.pkl                │
│                                                              │
│  predictor.py                                               │
│  ├─ loads per-CR TFLite model                               │
│  ├─ handles numpy compatibility                             │
│  └─ runs inference()                                        │
│                                                              │
│  scaler.py                                                  │
│  ├─ calculateReplicas() — predicted_rps / capacity         │
│  ├─ applyRateLimits() — prevent jumps > 2× or < 0.5×      │
│  ├─ stabilizationFilter() — require 2 consecutive agree    │
│  └─ patchDeployment() — kubectl patch replicas            │
│                                                              │
│  config.py                                                  │
│  └─ environment variable configuration                      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
         ↓                                        ↑
    Query Metrics                          Patch Replicas
    (&& labels)                                  
         ↓                                        ↑
┌──────────────────────────────────────┐        │
│ Prometheus (15s scrape)              │        │
│ metrics: rps, cpu, memory, latency   │        │
│ range [now-6min, now]                │        │
└──────────────────────────────────────┘
                                                │
                              ┌─────────────────┘
                              ↓
                    ┌─────────────────────┐
                    │ Kubernetes Cluster  │
                    ├─────────────────────┤
                    │ Target Deployment   │
                    │ (test-app)          │
                    │ ...                 │
                    │ serving traffic     │
                    └─────────────────────┘
```

---

## Next Steps

1. **[Read Architecture](./architecture.md)** — Understand system design and data flow
2. **[Follow Deployment Guide](./deployment.md)** — Deploy operator to your cluster
3. **[Configure CR](./api.md)** — Customize for your application
4. **[Monitor & Operate](./commands.md)** — Run operator and watch autoscaling

---

## Support & References

- **Model Training** → See [ML Pipeline Docs](../architecture/ml_pipeline.md)
- **Prometheus Queries** → See [Queries Reference](../reference/working_queries.md)
- **Kubernetes CRD** → See [API Reference](./api.md)
- **Common Issues** → See [Troubleshooting](./troubleshooting.md)

